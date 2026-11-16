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

import nexus_links
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
        self.quarantine = self.root / "quarantine"
        self.servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = []
        self.base_url = self.start_server(self.downloads)

    def start_server(self, downloads: Path | None) -> str:
        handler = dashboard_server.make_handler(
            downloads,
            self.dashboard,
            blacklist=self.blacklist,
            config={"max_mod_id": 40000},
            quarantine=self.quarantine,
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # Короткий опрос: иначе каждое shutdown() ждёт полсекунды и тесты идут вдвое дольше.
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.servers.append((httpd, thread))
        return f"http://127.0.0.1:{httpd.server_port}"

    def tearDown(self) -> None:
        for httpd, thread in self.servers:
            httpd.shutdown()
            httpd.server_close()
            thread.join()
        self.temp_dir.cleanup()

    def make_entry(self, name: str, data: bytes = b"identical archive") -> dict:
        candidate = self.downloads / name
        keeper = self.downloads / "keeper.zip"
        candidate.write_bytes(data)
        keeper.write_bytes(data)
        stat = candidate.stat()
        return {
            "path": str(candidate),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": nexus_links.sha256_file(candidate),
            "reason": nexus_links.EXACT_DUP,
            "keeper": str(keeper),
        }

    def tree(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def preview(self, files: list[dict]) -> dict:
        status, result = self.post_json("/api/clean/preview", {"files": files})
        self.assertEqual(status, 200)
        return result

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
        dashboard = dashboard_server.resolve_dashboard(
            config,
            ["cli-downloads", "reports/cli.html"],
            root=self.root,
        )

        self.assertEqual(dashboard, self.root / "reports" / "cli.html")
        self.assertEqual(
            dashboard_server.resolve_dashboard(config, [], root=self.root),
            self.root / "configured.html",
        )
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
            "/api/clean/preview",
            {"files": []},
            origin="https://attacker.example",
        )
        self.assertEqual((status, result["status"]), (403, "forbidden_origin"))
        status, result = self.post_json("/api/clean/preview", ["not", "an", "object"])
        self.assertEqual((status, result["status"]), (400, "bad_request"))

    def test_direct_delete_endpoint_is_gone(self) -> None:
        candidate = self.downloads / "mod.zip"
        candidate.write_bytes(b"mod")

        status, result = self.post_json("/api/delete", {"path": str(candidate)})

        self.assertEqual((status, result["status"]), (410, "gone"))
        self.assertTrue(candidate.is_file())

    def test_server_and_dashboard_have_no_irreversible_delete_path(self) -> None:
        server_source = Path(dashboard_server.__file__).read_text(encoding="utf-8")
        template = nexus_links.TEMPLATE_PATH.read_text(encoding="utf-8")

        for needle in ("os.remove", "os.unlink", ".unlink(", "rmtree", "send2trash"):
            with self.subTest(source="server.py", needle=needle):
                self.assertNotIn(needle, server_source)
        for needle in ("/api/delete", "del /f", "x-bat", "delete_exact_duplicates",
                       "btnDeleteDups", "btnDownloadBat"):
            with self.subTest(source="dashboard_template.html", needle=needle):
                self.assertNotIn(needle, template)

    def test_clean_preview_reports_the_plan_and_changes_nothing(self) -> None:
        good = self.make_entry("copy.zip")
        missing = dict(good, path=str(self.downloads / "missing.zip"))
        before = self.tree()

        result = self.preview([good, missing])

        self.assertEqual(self.tree(), before)
        self.assertFalse(self.quarantine.exists())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [(item["path"], item["problem"]) for item in result["files"]],
            [(good["path"], None), (missing["path"], "файл не найден")],
        )
        self.assertEqual((result["ok_count"], result["skipped_count"]), (1, 1))
        self.assertEqual(result["total_size"], good["size"])
        self.assertEqual(result["quarantine"], str(self.quarantine))
        self.assertTrue(result["confirm"])

    def test_clean_apply_moves_previewed_files_to_quarantine(self) -> None:
        entry = self.make_entry("copy.zip")
        confirm = self.preview([entry])["confirm"]

        status, result = self.post_json(
            "/api/clean/apply", {"files": [entry], "confirm": confirm})

        self.assertEqual((status, result["status"]), (200, "ok"))
        self.assertFalse(Path(entry["path"]).exists())
        self.assertTrue(Path(entry["keeper"]).is_file())
        self.assertEqual([item["path"] for item in result["moved"]], [entry["path"]])
        destination = Path(result["moved"][0]["dest"])
        self.assertEqual(destination.read_bytes(), b"identical archive")
        self.assertEqual(destination.parent.parent, self.quarantine)
        self.assertEqual(result["moved_size"], entry["size"])
        self.assertIn("MOVE\t", Path(result["log"]).read_text(encoding="utf-8"))
        self.assertEqual((result["skipped"], result["failed"]), ([], []))

    def test_clean_skips_file_changed_after_scan(self) -> None:
        resized = dict(self.make_entry("resized.zip"), size=1)
        touched = self.make_entry("touched.zip")
        touched["mtime"] -= 60
        good = self.make_entry("copy.zip")
        files = [resized, touched, good]
        confirm = self.preview(files)["confirm"]

        status, result = self.post_json(
            "/api/clean/apply", {"files": files, "confirm": confirm})

        self.assertEqual(status, 200)
        self.assertEqual([item["path"] for item in result["moved"]], [good["path"]])
        self.assertEqual(
            [(item["path"], item["problem"]) for item in result["skipped"]],
            [(resized["path"], "файл изменился после сканирования"),
             (touched["path"], "файл изменился после сканирования")],
        )
        self.assertTrue(Path(resized["path"]).is_file())
        self.assertTrue(Path(touched["path"]).is_file())

    def test_clean_rejects_file_outside_downloads_and_non_archives(self) -> None:
        outside_dir = self.root / "downloads-other"
        outside_dir.mkdir()
        outside = self.make_entry("copy.zip")
        outside_file = outside_dir / "copy.zip"
        outside_file.write_bytes(b"identical archive")
        outside["path"] = str(outside_file)
        outside["mtime"] = outside_file.stat().st_mtime
        document = self.make_entry("notes.txt")
        files = [outside, document]
        preview = self.preview(files)

        status, result = self.post_json(
            "/api/clean/apply", {"files": files, "confirm": preview["confirm"]})

        self.assertEqual(
            [item["problem"] for item in preview["files"]],
            ["вне папки Downloads", "не архив мода"],
        )
        self.assertEqual((status, result["moved"]), (200, []))
        self.assertTrue(outside_file.is_file())
        self.assertTrue(Path(document["path"]).is_file())
        self.assertFalse(self.quarantine.exists())

    def test_clean_apply_requires_the_confirmation_of_the_same_preview(self) -> None:
        entry = self.make_entry("copy.zip")
        other = self.make_entry("other.zip")
        confirm = self.preview([entry])["confirm"]

        for payload in (
            {"files": [entry]},
            {"files": [entry], "confirm": "0" * 64},
            {"files": [entry, other], "confirm": confirm},
        ):
            with self.subTest(payload=sorted(payload)):
                status, result = self.post_json("/api/clean/apply", payload)
                self.assertEqual((status, result["status"]), (409, "preview_required"))

        self.assertTrue(Path(entry["path"]).is_file())
        self.assertTrue(Path(other["path"]).is_file())
        self.assertFalse(self.quarantine.exists())

    def test_clean_rejects_malformed_selection(self) -> None:
        for payload in ({}, {"files": "all"}, {"files": [None]}, {"files": ["path"]}):
            with self.subTest(payload=payload):
                status, result = self.post_json("/api/clean/preview", payload)
                self.assertEqual((status, result["status"]), (400, "bad_request"))

    def test_clean_is_refused_when_downloads_is_not_configured(self) -> None:
        entry = self.make_entry("copy.zip")
        self.base_url = self.start_server(None)

        for path in ("/api/clean/preview", "/api/clean/apply"):
            with self.subTest(path=path):
                status, result = self.post_json(path, {"files": [entry], "confirm": "x"})
                self.assertEqual((status, result["status"]), (409, "downloads_not_configured"))

        self.assertTrue(Path(entry["path"]).is_file())

    def test_missing_config_does_not_fall_back_to_the_home_downloads_folder(self) -> None:
        with patch("nexus_links.find_cyberpunk_install", return_value=None):
            settings = dashboard_server.load_core_settings(
                [], config_path=self.root / "missing.json")
            from_argument = dashboard_server.load_core_settings(
                [str(self.downloads)], config_path=self.root / "missing.json")

        self.assertIsNone(settings.downloads)
        self.assertEqual(from_argument.downloads, self.downloads)
        self.assertEqual(
            from_argument.quarantine, self.downloads / nexus_links.QUARANTINE_DIRNAME)

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
