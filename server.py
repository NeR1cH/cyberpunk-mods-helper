"""Локальный сервер дашборда. Запуск: python server.py [папка_Downloads] [файл_дашборда.html]"""

from __future__ import annotations

from argparse import Namespace
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from json_files import atomic_write_json
import nexus_links

PORT = 8000
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
MAX_CLEAN_ENTRIES = 5000
MAX_BODY_BYTES = 1024 * 1024
TOKEN_HEADER = "X-ModAudit-Token"
# Шаблон дашборда содержит пустой meta; сервер вписывает в него токен при отдаче страницы.
TOKEN_PLACEHOLDER = b'<meta name="api-token" content="">'


def resolve_dashboard(
    config: dict[str, Any],
    arguments: list[str],
    root: Path = ROOT,
) -> Path:
    html_arg = arguments[1] if len(arguments) > 1 else None
    html_config = config.get("out")
    dashboard = Path(html_arg or html_config or root / "mod_audit_dashboard.html")
    if not dashboard.is_absolute():
        dashboard = root / dashboard
    return dashboard


def load_core_settings(
    arguments: list[str],
    config_path: Path = CONFIG_PATH,
) -> nexus_links.Settings:
    """Папки для очистки — из того же load_settings, что у команд scan и clean.

    Если Downloads не задана ни аргументом, ни в config.json, она остаётся None
    и очистка из дашборда отключается: угадывать ~/Downloads нельзя.
    """
    return nexus_links.load_settings(Namespace(
        config=str(config_path),
        downloads=arguments[0] if arguments else None,
    ))


def inside_roots(
    path: str | os.PathLike[str],
    roots: Iterable[str | os.PathLike[str]],
) -> bool:
    """Лежит ли путь внутри одной из папок (или совпадает с ней) — по настоящим путям."""
    real = os.path.normcase(os.path.realpath(path))
    for root in roots:
        base = os.path.normcase(os.path.realpath(root))
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except ValueError:   # разные диски
            continue
    return False


def insert_token(page: bytes, token: str) -> bytes:
    """Вписывает токен запуска в отдаваемую страницу; файл отчёта на диске не меняется."""
    meta = f'<meta name="api-token" content="{token}">'.encode("ascii")
    if TOKEN_PLACEHOLDER in page:
        return page.replace(TOKEN_PLACEHOLDER, meta, 1)
    head = page.lower().find(b"<head>")
    if head < 0:
        return meta + page
    cut = head + len(b"<head>")
    return page[:cut] + meta + page[cut:]


def make_handler(
    downloads: Path | None,
    dashboard: Path,
    blacklist: Path = BLACKLIST_PATH,
    config: dict[str, Any] | None = None,
    port: int | None = None,
    quarantine: Path | None = None,
    reveal_roots: Iterable[Path | None] = (),
) -> type[BaseHTTPRequestHandler]:
    settings = config or {}
    if quarantine is None and downloads is not None:
        quarantine = downloads / nexus_links.QUARANTINE_DIRNAME
    # «Показать в Проводнике» работает только внутри известных проекту папок.
    allowed_reveal = [root for root in (downloads, quarantine, *reveal_roots) if root is not None]
    # Токен на запуск: знает его только страница, которую отдал этот сервер.
    api_token = secrets.token_urlsafe(32)
    # Подтверждение привязывает apply к тому списку, который показал предпросмотр.
    clean_secret = secrets.token_bytes(32)
    clean_lock = threading.Lock()

    def clean_confirmation(entries: list[dict[str, Any]]) -> str:
        canonical = json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hmac.new(clean_secret, canonical, hashlib.sha256).hexdigest()

    def entry_path(entry: dict[str, Any]) -> str:
        path = entry.get("path")
        return path if isinstance(path, str) else ""

    class DashboardHandler(BaseHTTPRequestHandler):
        def reply(
            self,
            code: int,
            body: bytes,
            content_type: str = "application/json; charset=utf-8",
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def local_port(self) -> int:
            return port if port is not None else self.server.server_address[1]

        def host_is_local(self) -> bool:
            """Защита от DNS rebinding: имя хоста в запросе должно быть локальным."""
            host = (self.headers.get("Host") or "").strip().lower()
            return host in {f"localhost:{self.local_port()}", f"127.0.0.1:{self.local_port()}"}

        def origin_is_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                # Без Origin верим только явной отметке браузера «запрос с той же страницы».
                return self.headers.get("Sec-Fetch-Site") == "same-origin"
            return origin in {f"http://localhost:{self.local_port()}",
                              f"http://127.0.0.1:{self.local_port()}"}

        def read_body(self) -> bytes | None:
            """Тело запроса не больше MAX_BODY_BYTES; иначе отвечает сам и возвращает None."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self.reply_json(400, "bad_request")
                return None
            if length > MAX_BODY_BYTES:
                self.reply_json(413, "payload_too_large")
                return None
            return self.rfile.read(length)

        def reply_json(self, code: int, status: str) -> None:
            self.reply_data(code, {"status": status})

        def reply_data(self, code: int, data: dict[str, Any]) -> None:
            self.reply(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def do_GET(self) -> None:
            if not self.host_is_local():
                self.reply_json(403, "forbidden_host")
                return
            if self.path.split("?", 1)[0] not in ("/", "/index.html"):
                self.reply_json(404, "not_found")
                return
            if not dashboard.is_file():
                self.reply_json(404, "dashboard_not_generated")
                return
            # no-store: после перезапуска сервера страница из кэша несла бы старый токен.
            self.reply(200, insert_token(dashboard.read_bytes(), api_token),
                       "text/html; charset=utf-8", {"Cache-Control": "no-store"})

        def do_OPTIONS(self) -> None:
            # Чужим страницам preflight не разрешаем, своей он не нужен.
            self.reply(405, json.dumps({"status": "method_not_allowed"}).encode("utf-8"),
                       extra_headers={"Allow": "GET, POST"})

        def do_POST(self) -> None:
            if not self.host_is_local():
                self.reply_json(403, "forbidden_host")
                return
            if not self.origin_is_allowed():
                self.reply_json(403, "forbidden_origin")
                return
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                self.reply_json(415, "unsupported_media_type")
                return
            supplied_token = self.headers.get(TOKEN_HEADER) or ""
            if not hmac.compare_digest(supplied_token.encode("utf-8"), api_token.encode("utf-8")):
                self.reply_json(403, "bad_token")
                return

            raw = self.read_body()
            if raw is None:
                return
            if self.path == "/api/delete":
                # Прямое удаление убрано: очистка идёт только через предпросмотр и карантин.
                self.reply_json(410, "gone")
                return
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("JSON object expected")
            except (ValueError, TypeError):
                self.reply_json(400, "bad_request")
                return

            if self.path == "/api/blacklist":
                self.handle_blacklist(payload)
                return
            if self.path in {"/api/clean/preview", "/api/clean/apply"}:
                self.handle_clean(payload, apply=self.path.endswith("/apply"))
                return

            if self.path != "/api/reveal":
                self.reply_json(404, "not_found")
                return

            path = payload.get("path")
            if not isinstance(path, str) or not path:
                self.reply_json(400, "bad_request")
                return
            self.handle_reveal(path)

        def handle_blacklist(self, payload: dict[str, Any]) -> None:
            action = payload.get("action")
            mod_id = payload.get("mod_id")
            max_mod_id = nexus_links.configured_max_mod_id(settings)
            if (
                not isinstance(mod_id, int)
                or isinstance(mod_id, bool)
                or not 1 <= mod_id <= max_mod_id
                or action not in ("add", "remove")
            ):
                self.reply_json(400, "bad_request")
                return

            reason = payload.get("reason", "")
            if action == "add" and (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason.strip()) > 500
            ):
                self.reply_json(400, "bad_reason")
                return
            if action == "remove" and mod_id in nexus_links.OUTDATED_OR_DANGEROUS:
                self.reply_json(403, "builtin_entry")
                return

            try:
                if blacklist.is_file():
                    with blacklist.open(encoding="utf-8-sig") as blacklist_file:
                        entries = json.load(blacklist_file)
                else:
                    entries = {}
                if not isinstance(entries, dict):
                    self.reply_json(500, "blacklist_invalid")
                    return
            except (OSError, ValueError) as exc:
                print(f"Не удалось прочитать blacklist.json: {exc}", file=sys.stderr)
                self.reply_json(500, "blacklist_invalid")
                return

            key = str(mod_id)
            if action == "add":
                entries[key] = {"reason": reason.strip()}
            else:
                entries.pop(key, None)

            try:
                atomic_write_json(blacklist, entries)
            except OSError as exc:
                print(f"Не удалось сохранить blacklist.json: {exc}", file=sys.stderr)
                self.reply_json(500, "blacklist_write_failed")
                return
            self.reply_json(200, "ok")

        def handle_reveal(self, path: str) -> None:
            # Сначала границы, потом существование: о файлах вне папок проекта не сообщаем ничего.
            if "\0" in path or not inside_roots(path, allowed_reveal):
                self.reply_json(403, "outside_allowed_folders")
                return
            if not os.path.exists(path):
                self.reply_json(404, "file_not_found")
                return
            target = os.path.normpath(path)
            try:
                if os.path.isdir(target):
                    subprocess.Popen(["explorer", target])
                else:
                    # Одной строкой: Проводник ждёт именно /select,"путь" и тогда выделяет файл.
                    subprocess.Popen(f'explorer /select,"{target}"')
            except OSError as exc:
                print(f"Не удалось открыть {target} в Проводнике: {exc}", file=sys.stderr)
                self.reply_json(500, "reveal_failed")
                return
            self.reply_json(200, "ok")

        def handle_clean(self, payload: dict[str, Any], apply: bool) -> None:
            """Предпросмотр и перенос в карантин: тот же формат и те же проверки, что у команды clean."""
            if downloads is None or quarantine is None:
                self.reply_json(409, "downloads_not_configured")
                return
            entries = payload.get("files")
            if (
                not isinstance(entries, list)
                or len(entries) > MAX_CLEAN_ENTRIES
                or any(not isinstance(entry, dict) for entry in entries)
            ):
                self.reply_json(400, "bad_request")
                return
            confirm = clean_confirmation(entries)

            if not apply:
                checks = list(nexus_links.iter_clean_checks(entries, downloads, quarantine))
                accepted = [entry for entry, problem in checks if problem is None]
                self.reply_data(200, {
                    "status": "ok",
                    "confirm": confirm,
                    "quarantine": str(quarantine),
                    "files": [
                        {"path": entry_path(entry), "size": entry.get("size"), "problem": problem}
                        for entry, problem in checks
                    ],
                    "ok_count": len(accepted),
                    "skipped_count": len(checks) - len(accepted),
                    "total_size": sum(entry["size"] for entry in accepted),
                })
                return

            supplied = payload.get("confirm")
            if not isinstance(supplied, str) or not hmac.compare_digest(
                    supplied.encode("utf-8"), confirm.encode("utf-8")):
                self.reply_json(409, "preview_required")
                return
            if not clean_lock.acquire(blocking=False):
                self.reply_json(409, "clean_in_progress")
                return
            try:
                # Проверяем заново: между предпросмотром и применением файлы могли измениться.
                checks = list(nexus_links.iter_clean_checks(entries, downloads, quarantine))
                plan = [entry for entry, problem in checks if problem is None]
                outcome = nexus_links.CleanOutcome()
                if plan:
                    nexus_links._apply_clean_plan(plan, quarantine, "quarantine", outcome)
            except OSError as exc:
                print(f"Не удалось выполнить перенос в карантин: {exc}", file=sys.stderr)
                self.reply_json(500, "clean_failed")
                return
            finally:
                clean_lock.release()

            sizes = {entry_path(entry): entry["size"] for entry in plan}
            self.reply_data(200, {
                "status": "partial" if outcome.failed else "ok",
                "moved": [{"path": source, "dest": dest} for source, dest in outcome.moved],
                "moved_size": sum(sizes.get(source, 0) for source, _ in outcome.moved),
                "failed": [{"path": source, "problem": error} for source, error in outcome.failed],
                "skipped": [
                    {"path": entry_path(entry), "problem": problem}
                    for entry, problem in checks if problem is not None
                ],
                "run_dir": str(outcome.run_dir) if outcome.run_dir else "",
                "log": str(outcome.log_path) if outcome.log_path else "",
            })

    DashboardHandler.api_token = api_token   # для тестов; в ответах сервера токена нет, кроме самой страницы
    return DashboardHandler


def main(arguments: list[str] | None = None) -> None:
    args = sys.argv[1:] if arguments is None else arguments
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        core = load_core_settings(args)
    except nexus_links.ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    config = core.cfg or {}
    dashboard = resolve_dashboard(config, args)
    downloads = core.downloads
    handler = make_handler(
        downloads, dashboard, config=config, port=PORT, quarantine=core.quarantine,
        reveal_roots=[core.vortex, core.game],
    )
    if downloads is None:
        print("Папка downloads не задана (config.json или первый аргумент): "
              "очистка из дашборда отключена.")
    print(f"Дашборд: http://localhost:{PORT}  |  Downloads: {downloads or 'не задана'}")
    webbrowser.open(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
