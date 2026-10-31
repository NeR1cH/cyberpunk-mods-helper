from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server as dashboard_server


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.dashboard = self.root / "dashboard.html"
        self.dashboard.write_text("<h1>Dashboard</h1>", encoding="utf-8")
        self.blacklist = self.root / "blacklist.json"
        handler = dashboard_server.make_handler(
            self.downloads,
            self.dashboard,
            blacklist=self.blacklist,
            config={"max_mod_id": 40000},
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.temp_dir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = headers or {}
        if data is not None:
            request_headers = {"Content-Type": "application/json", **request_headers}
        request = Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def post_json(self, path: str, payload: object, origin: str | None = None) -> tuple[int, dict]:
        headers = {"Origin": origin} if origin is not None else None
        status, body = self.request("POST", path, payload, headers)
        return status, json.loads(body)

    def test_config_loading_and_path_resolution(self) -> None:
        config_path = self.root / "settings.json"
        config_path.write_text(
            json.dumps({"downloads": "configured-downloads", "out": "configured.html"}),
            encoding="utf-8",
        )
        config = dashboard_server.load_config(config_path)
        downloads, dashboard = dashboard_server.resolve_paths(
            config,
            ["cli-downloads", "reports/cli.html"],
            root=self.root,
        )

        self.assertEqual(downloads, Path("cli-downloads").resolve())
        self.assertEqual(dashboard, self.root / "reports" / "cli.html")
        self.assertEqual(dashboard_server.load_config(self.root / "missing.json"), {})

        config_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            dashboard_server.load_config(config_path)

    def test_download_path_check_uses_directory_boundaries(self) -> None:
        self.assertFalse(dashboard_server.in_downloads(self.downloads, self.downloads))
        self.assertTrue(dashboard_server.in_downloads(self.downloads / "mod.zip", self.downloads))
        self.assertFalse(
            dashboard_server.in_downloads(
                self.root / "downloads-other" / "mod.zip",
                self.downloads,
            )
        )

    def test_dashboard_is_served_only_for_document_routes(self) -> None:
        status, body = self.request("GET", "/?refresh=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<h1>Dashboard</h1>")
        self.assertEqual(self.request("GET", "/missing")[0], 404)

    def test_post_rejects_foreign_origin_and_non_object_json(self) -> None:
        status, result = self.post_json(
            "/api/delete",
            {"path": str(self.downloads / "mod.zip")},
            origin="https://attacker.example",
        )
        self.assertEqual((status, result["status"]), (403, "forbidden_origin"))
        status, result = self.post_json("/api/delete", ["not", "an", "object"])
        self.assertEqual((status, result["status"]), (400, "bad_request"))

    def test_delete_is_limited_to_files_inside_downloads(self) -> None:
        candidate = self.downloads / "mod.zip"
        candidate.write_bytes(b"mod")
        status, result = self.post_json("/api/delete", {"path": str(candidate)})
        self.assertEqual((status, result["status"]), (200, "ok"))
        self.assertFalse(candidate.exists())

        outside = self.root / "downloads-other" / "mod.zip"
        outside.parent.mkdir()
        outside.write_bytes(b"keep")
        status, result = self.post_json("/api/delete", {"path": str(outside)})
        self.assertEqual((status, result["status"]), (403, "outside_downloads"))
        self.assertTrue(outside.exists())

    def test_blacklist_add_remove_and_reject_invalid_entries(self) -> None:
        status, result = self.post_json(
            "/api/blacklist",
            {"action": "add", "mod_id": 1234, "reason": "Manual review"},
        )
        self.assertEqual((status, result["status"]), (200, "ok"))
        self.assertEqual(
            json.loads(self.blacklist.read_text(encoding="utf-8")),
            {"1234": {"reason": "Manual review"}},
        )

        status, result = self.post_json(
            "/api/blacklist",
            {"action": "add", "mod_id": 1234, "reason": " "},
        )
        self.assertEqual((status, result["status"]), (400, "bad_reason"))

        status, result = self.post_json(
            "/api/blacklist",
            {"action": "remove", "mod_id": 786},
        )
        self.assertEqual((status, result["status"]), (403, "builtin_entry"))

        status, result = self.post_json(
            "/api/blacklist",
            {"action": "remove", "mod_id": 1234},
        )
        self.assertEqual((status, result["status"]), (200, "ok"))
        self.assertEqual(json.loads(self.blacklist.read_text(encoding="utf-8")), {})

    def test_reveal_opens_existing_path(self) -> None:
        candidate = self.downloads / "mod.zip"
        candidate.write_bytes(b"mod")
        with patch("server.subprocess.Popen") as popen:
            status, result = self.post_json(
                "/api/reveal",
                {"path": str(candidate)},
            )

        self.assertEqual((status, result["status"]), (200, "ok"))
        popen.assert_called_once_with(["explorer", str(self.downloads)])


if __name__ == "__main__":
    unittest.main()
