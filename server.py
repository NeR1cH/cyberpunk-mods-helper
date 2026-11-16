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
from typing import Any

from json_files import atomic_write_json
import nexus_links

PORT = 8000
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
DEFAULT_MAX_MOD_ID = 35000
BUILTIN_MOD_IDS = {786, 3560, 4088}
MAX_CLEAN_ENTRIES = 5000


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError(f"В {path} ожидается JSON-объект.")
    return config


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


def in_downloads(path: str | os.PathLike[str], downloads: str | os.PathLike[str]) -> bool:
    try:
        real = os.path.normcase(os.path.realpath(path))
        base = os.path.normcase(os.path.realpath(downloads))
        return os.path.commonpath([real, base]) == base and real != base
    except ValueError:
        return False


def make_handler(
    downloads: Path | None,
    dashboard: Path,
    blacklist: Path = BLACKLIST_PATH,
    config: dict[str, Any] | None = None,
    port: int = PORT,
    quarantine: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    settings = config or {}
    origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    if quarantine is None and downloads is not None:
        quarantine = downloads / nexus_links.QUARANTINE_DIRNAME
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
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply_json(self, code: int, status: str) -> None:
            self.reply_data(code, {"status": status})

        def reply_data(self, code: int, data: dict[str, Any]) -> None:
            self.reply(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] not in ("/", "/index.html"):
                self.reply_json(404, "not_found")
                return
            if not dashboard.is_file():
                self.reply_json(404, "dashboard_not_generated")
                return
            self.reply(200, dashboard.read_bytes(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            if origin and origin not in origins:
                self.reply_json(403, "forbidden_origin")
                return

            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            except (ValueError, TypeError):
                self.reply_json(400, "bad_request")
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
            max_mod_id = settings.get("max_mod_id", DEFAULT_MAX_MOD_ID)
            if (
                not isinstance(max_mod_id, int)
                or isinstance(max_mod_id, bool)
                or max_mod_id < 1
            ):
                max_mod_id = DEFAULT_MAX_MOD_ID
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
            if action == "remove" and mod_id in BUILTIN_MOD_IDS:
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
            if not os.path.exists(path):
                self.reply_json(404, "file_not_found")
                return
            target = os.path.normpath(path)
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            try:
                subprocess.Popen(["explorer", folder])
            except OSError as exc:
                print(f"Не удалось открыть папку {folder}: {exc}", file=sys.stderr)
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

    return DashboardHandler


def main(arguments: list[str] | None = None) -> None:
    args = sys.argv[1:] if arguments is None else arguments
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config()
    dashboard = resolve_dashboard(config, args)
    core = load_core_settings(args)
    downloads = core.downloads
    handler = make_handler(downloads, dashboard, config=config, quarantine=core.quarantine)
    if downloads is None:
        print("Папка downloads не задана (config.json или первый аргумент): "
              "очистка из дашборда отключена.")
    print(f"Дашборд: http://localhost:{PORT}  |  Downloads: {downloads or 'не задана'}")
    webbrowser.open(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
