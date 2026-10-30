"""Локальный сервер дашборда. Запуск: python server.py [папка_Downloads] [файл_дашборда.html]"""
import json, os, subprocess, sys, tempfile, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
BLACKLIST_PATH = os.path.join(ROOT, "blacklist.json")
DEFAULT_MAX_MOD_ID = 35000
if os.path.isfile(CONFIG_PATH):
    with open(CONFIG_PATH, encoding="utf-8-sig") as config_file:
        CONFIG = json.load(config_file)
    if not isinstance(CONFIG, dict):
        raise ValueError(f"В {CONFIG_PATH} ожидается JSON-объект.")
else:
    CONFIG = {}

downloads_arg = sys.argv[1] if len(sys.argv) > 1 else None
downloads_config = CONFIG.get("downloads")
DOWNLOADS = os.path.realpath(
    downloads_arg or downloads_config or os.path.expanduser("~/Downloads")
)

html_arg = sys.argv[2] if len(sys.argv) > 2 else None
html_config = CONFIG.get("out")
HTML = html_arg or html_config or os.path.join(ROOT, "mod_audit_dashboard.html")
if not os.path.isabs(HTML):
    HTML = os.path.join(ROOT, HTML)

ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}


def in_downloads(path):
    try:
        real, base = os.path.normcase(os.path.realpath(path)), os.path.normcase(DOWNLOADS)
        return os.path.commonpath([real, base]) == base and real != base
    except ValueError:  # путь на другом диске
        return False


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, code, status):
        self.reply(code, json.dumps({"status": status}).encode())

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            return self.reply_json(404, "not_found")
        if not os.path.isfile(HTML):
            return self.reply_json(404, "dashboard_not_generated")
        with open(HTML, "rb") as f:
            self.reply(200, f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin not in ORIGINS:  # защита от запросов с чужих сайтов
            return self.reply_json(403, "forbidden_origin")
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("JSON object expected")
        except (ValueError, TypeError):
            return self.reply_json(400, "bad_request")

        if self.path == "/api/blacklist":
            action = payload.get("action")
            mod_id = payload.get("mod_id")
            max_mod_id = CONFIG.get("max_mod_id", DEFAULT_MAX_MOD_ID)
            if (not isinstance(max_mod_id, int) or isinstance(max_mod_id, bool)
                    or max_mod_id < 1):
                max_mod_id = DEFAULT_MAX_MOD_ID
            if (not isinstance(mod_id, int) or isinstance(mod_id, bool)
                    or not 1 <= mod_id <= max_mod_id or action not in ("add", "remove")):
                return self.reply_json(400, "bad_request")

            reason = payload.get("reason", "")
            if action == "add" and (
                    not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500):
                return self.reply_json(400, "bad_reason")
            if action == "remove" and mod_id in {786, 3560, 4088}:
                return self.reply_json(403, "builtin_entry")

            try:
                if os.path.isfile(BLACKLIST_PATH):
                    with open(BLACKLIST_PATH, encoding="utf-8-sig") as blacklist_file:
                        entries = json.load(blacklist_file)
                else:
                    entries = {}
                if not isinstance(entries, dict):
                    return self.reply_json(500, "blacklist_invalid")
            except (OSError, ValueError) as exc:
                print(f"Не удалось прочитать blacklist.json: {exc}", file=sys.stderr)
                return self.reply_json(500, "blacklist_invalid")

            key = str(mod_id)
            if action == "add":
                entries[key] = {"reason": reason.strip()}
            else:
                entries.pop(key, None)

            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=".blacklist-", suffix=".tmp", dir=os.path.dirname(BLACKLIST_PATH) or ".")
                with os.fdopen(fd, "w", encoding="utf-8") as blacklist_file:
                    json.dump(entries, blacklist_file, ensure_ascii=False, indent=2)
                    blacklist_file.write("\n")
                os.replace(temp_path, BLACKLIST_PATH)
            except OSError as exc:
                print(f"Не удалось сохранить blacklist.json: {exc}", file=sys.stderr)
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError as cleanup_exc:
                        print(f"Не удалось удалить временный файл {temp_path}: {cleanup_exc}",
                              file=sys.stderr)
                return self.reply_json(500, "blacklist_write_failed")
            return self.reply_json(200, "ok")

        try:
            path = payload["path"]
            if not isinstance(path, str) or not path:
                raise ValueError("path expected")
        except (KeyError, TypeError, ValueError):
            return self.reply_json(400, "bad_request")

        if self.path == "/api/reveal":
            if not os.path.exists(path):
                return self.reply_json(404, "file_not_found")
            target = os.path.normpath(path)
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            try:
                subprocess.Popen(["explorer", folder])
            except OSError as exc:
                print(f"Не удалось открыть папку {folder}: {exc}", file=sys.stderr)
                return self.reply_json(500, "reveal_failed")
            return self.reply_json(200, "ok")

        if self.path == "/api/delete":
            if not in_downloads(path):
                return self.reply_json(403, "outside_downloads")
            if not os.path.isfile(path):
                return self.reply_json(404, "file_not_found")
            try:
                os.remove(path)
            except OSError:
                return self.reply_json(500, "delete_failed")
            return self.reply_json(200, "ok")

        self.reply_json(404, "not_found")


if __name__ == "__main__":
    print(f"Дашборд: http://localhost:{PORT}  |  Downloads: {DOWNLOADS}")
    webbrowser.open(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
