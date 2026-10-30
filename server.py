"""Локальный сервер дашборда. Запуск: python server.py [папка_Downloads] [файл_дашборда.html]"""
import json, os, subprocess, sys, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
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
            path = json.loads(raw)["path"]
            assert isinstance(path, str) and path
        except (ValueError, KeyError, TypeError, AssertionError):
            return self.reply_json(400, "bad_request")

        if self.path == "/api/reveal":
            if not os.path.exists(path):
                return self.reply_json(404, "file_not_found")
            subprocess.run(["explorer", f"/select,{os.path.normpath(path)}"])  # explorer всегда возвращает 1
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
