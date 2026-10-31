"""Локальный сервер дашборда. Запуск: python server.py [папка_Downloads] [файл_дашборда.html]"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PORT = 8000
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
DEFAULT_MAX_MOD_ID = 35000
BUILTIN_MOD_IDS = {786, 3560, 4088}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError(f"В {path} ожидается JSON-объект.")
    return config


def resolve_paths(
    config: dict[str, Any],
    arguments: list[str],
    root: Path = ROOT,
) -> tuple[Path, Path]:
    downloads_arg = arguments[0] if arguments else None
    downloads_config = config.get("downloads")
    downloads = Path(os.path.realpath(
        downloads_arg or downloads_config or Path.home() / "Downloads"
    ))

    html_arg = arguments[1] if len(arguments) > 1 else None
    html_config = config.get("out")
    dashboard = Path(html_arg or html_config or root / "mod_audit_dashboard.html")
    if not dashboard.is_absolute():
        dashboard = root / dashboard
    return downloads, dashboard


def in_downloads(path: str | os.PathLike[str], downloads: str | os.PathLike[str]) -> bool:
    try:
        real = os.path.normcase(os.path.realpath(path))
        base = os.path.normcase(os.path.realpath(downloads))
        return os.path.commonpath([real, base]) == base and real != base
    except ValueError:
        return False


def make_handler(
    downloads: Path,
    dashboard: Path,
    blacklist: Path = BLACKLIST_PATH,
    config: dict[str, Any] | None = None,
    port: int = PORT,
) -> type[BaseHTTPRequestHandler]:
    settings = config or {}
    origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}

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
            self.reply(code, json.dumps({"status": status}).encode("utf-8"))

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
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("JSON object expected")
            except (ValueError, TypeError):
                self.reply_json(400, "bad_request")
                return

            if self.path == "/api/blacklist":
                self.handle_blacklist(payload)
                return

            if self.path not in {"/api/reveal", "/api/delete"}:
                self.reply_json(404, "not_found")
                return

            path = payload.get("path")
            if not isinstance(path, str) or not path:
                self.reply_json(400, "bad_request")
                return
            if self.path == "/api/reveal":
                self.handle_reveal(path)
            else:
                self.handle_delete(path)

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

            temp_path: str | None = None
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=".blacklist-",
                    suffix=".tmp",
                    dir=str(blacklist.parent),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as blacklist_file:
                    json.dump(entries, blacklist_file, ensure_ascii=False, indent=2)
                    blacklist_file.write("\n")
                os.replace(temp_path, blacklist)
            except OSError as exc:
                print(f"Не удалось сохранить blacklist.json: {exc}", file=sys.stderr)
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError as cleanup_exc:
                        print(
                            f"Не удалось удалить временный файл {temp_path}: {cleanup_exc}",
                            file=sys.stderr,
                        )
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

        def handle_delete(self, path: str) -> None:
            if not in_downloads(path, downloads):
                self.reply_json(403, "outside_downloads")
                return
            if not os.path.isfile(path):
                self.reply_json(404, "file_not_found")
                return
            try:
                os.remove(path)
            except OSError as exc:
                print(f"Не удалось удалить файл {path}: {exc}", file=sys.stderr)
                self.reply_json(500, "delete_failed")
                return
            self.reply_json(200, "ok")

    return DashboardHandler


def main(arguments: list[str] | None = None) -> None:
    args = sys.argv[1:] if arguments is None else arguments
    config = load_config()
    downloads, dashboard = resolve_paths(config, args)
    handler = make_handler(downloads, dashboard, config=config)
    print(f"Дашборд: http://localhost:{PORT}  |  Downloads: {downloads}")
    webbrowser.open(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
