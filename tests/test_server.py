from __future__ import annotations

import json
from pathlib import Path
import re
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
        self.vortex = self.root / "vortex"
        self.game = self.root / "game"
        self.servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = []
        self.base_url = self.start_server(self.downloads)

    def start_server(self, downloads: Path | None) -> str:
        handler = dashboard_server.make_handler(
            downloads,
            self.dashboard,
            blacklist=self.blacklist,
            config={"max_mod_id": 40000},
            quarantine=self.quarantine,
            reveal_roots=[self.vortex, self.game],
        )
        self.token = handler.api_token
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # Короткий опрос: иначе каждое shutdown() ждёт полсекунды и тесты идут вдвое дольше.
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.servers.append((httpd, thread))
        self.port = httpd.server_port
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
        request_headers = dict(headers or {})
        if data is not None:
            # То, что шлёт сам дашборд; значение None убирает заголовок из запроса.
            request_headers = {
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "X-ModAudit-Token": self.token,
                **request_headers,
            }
        request = Request(
            self.base_url + path,
            data=data,
            headers={name: value for name, value in request_headers.items() if value is not None},
            method=method,
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def post_json(
        self,
        path: str,
        payload: object,
        origin: str | None = None,
        headers: dict[str, str | None] | None = None,
    ) -> tuple[int, dict]:
        request_headers = dict(headers or {})
        if origin is not None:
            request_headers["Origin"] = origin
        status, body = self.request("POST", path, payload, request_headers)
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

    def test_folder_check_uses_directory_boundaries(self) -> None:
        roots = [self.downloads, self.game]
        self.assertTrue(dashboard_server.inside_roots(self.downloads, roots))
        self.assertTrue(dashboard_server.inside_roots(self.downloads / "mod.zip", roots))
        self.assertTrue(dashboard_server.inside_roots(self.game / "archive" / "pc", roots))
        self.assertFalse(
            dashboard_server.inside_roots(self.root / "downloads-other" / "mod.zip", roots))
        self.assertFalse(
            dashboard_server.inside_roots(self.downloads / ".." / "secret.txt", roots))
        self.assertFalse(dashboard_server.inside_roots(self.downloads / "mod.zip", []))

    def test_dashboard_is_served_only_for_document_routes(self) -> None:
        status, body = self.request("GET", "/?refresh=1")
        self.assertEqual(status, 200)
        self.assertTrue(body.endswith(b"<h1>Dashboard</h1>"))
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
        popen.assert_called_once_with(f'explorer /select,"{candidate}"')

    # ---------- S06: по одному тесту на каждое правило ----------

    def test_rule_1_host_header_must_be_local(self) -> None:
        for host in ("attacker.example", f"attacker.example:{self.port}", "127.0.0.1:1"):
            with self.subTest(host=host):
                status, body = self.request("GET", "/", headers={"Host": host})
                self.assertEqual((status, json.loads(body)["status"]), (403, "forbidden_host"))
                self.assertNotIn(self.token.encode(), body)
                status, result = self.post_json(
                    "/api/clean/preview", {"files": []}, headers={"Host": host})
                self.assertEqual((status, result["status"]), (403, "forbidden_host"))

        for host in (f"localhost:{self.port}", f"LOCALHOST:{self.port}", f"127.0.0.1:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/", headers={"Host": host})[0], 200)

    def test_rule_2_post_requires_json_content_type(self) -> None:
        for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
            with self.subTest(content_type=content_type):
                status, result = self.post_json(
                    "/api/clean/preview", {"files": []}, headers={"Content-Type": content_type})
                self.assertEqual((status, result["status"]), (415, "unsupported_media_type"))

        status, _ = self.post_json(
            "/api/clean/preview", {"files": []},
            headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)

    def test_rule_3_post_requires_allowed_origin_or_same_origin_fetch(self) -> None:
        rejected = (
            {"Origin": None},
            {"Origin": None, "Sec-Fetch-Site": "cross-site"},
            {"Origin": None, "Sec-Fetch-Site": "same-site"},
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Origin": "https://attacker.example", "Sec-Fetch-Site": "same-origin"},
        )
        for headers in rejected:
            with self.subTest(headers=headers):
                status, result = self.post_json("/api/clean/preview", {"files": []}, headers=headers)
                self.assertEqual((status, result["status"]), (403, "forbidden_origin"))

        accepted = (
            {"Origin": f"http://localhost:{self.port}"},
            {"Origin": f"http://127.0.0.1:{self.port}"},
            {"Origin": None, "Sec-Fetch-Site": "same-origin"},
        )
        for headers in accepted:
            with self.subTest(headers=headers):
                status, _ = self.post_json("/api/clean/preview", {"files": []}, headers=headers)
                self.assertEqual(status, 200)

    def test_rule_4_post_requires_the_token_from_the_served_page(self) -> None:
        self.dashboard.write_text(
            '<html><head><meta name="api-token" content=""></head><body>Отчёт</body></html>',
            encoding="utf-8",
        )
        _, page = self.request("GET", "/")
        served = re.search(r'<meta name="api-token" content="([^"]+)">', page.decode("utf-8"))

        self.assertIsNotNone(served)
        self.assertEqual(page.count(b'name="api-token"'), 1)
        self.assertNotIn(self.token, self.dashboard.read_text(encoding="utf-8"))
        for token in (None, "", "wrong", served.group(1) + "x"):
            with self.subTest(token=token):
                status, result = self.post_json(
                    "/api/clean/preview", {"files": []}, headers={"X-ModAudit-Token": token})
                self.assertEqual((status, result["status"]), (403, "bad_token"))
        status, _ = self.post_json(
            "/api/clean/preview", {"files": []}, headers={"X-ModAudit-Token": served.group(1)})
        self.assertEqual(status, 200)

        first_token = self.token
        self.start_server(self.downloads)
        self.assertNotEqual(self.token, first_token)
        self.assertGreaterEqual(len(self.token), 32)

    def test_rule_5_request_body_is_limited_to_one_megabyte(self) -> None:
        limit = dashboard_server.MAX_BODY_BYTES
        self.assertEqual(limit, 1024 * 1024)

        status, result = self.post_json(
            "/api/clean/preview", {"files": []}, headers={"Content-Length": str(limit + 1)})
        self.assertEqual((status, result["status"]), (413, "payload_too_large"))

        for length in ("-1", "many"):
            with self.subTest(length=length):
                status, result = self.post_json(
                    "/api/clean/preview", {"files": []}, headers={"Content-Length": length})
                self.assertEqual((status, result["status"]), (400, "bad_request"))

        padding = "x" * (limit - 200)
        status, _ = self.post_json("/api/clean/preview", {"files": [], "padding": padding})
        self.assertEqual(status, 200)

    def test_rule_6_reveal_is_limited_to_configured_folders(self) -> None:
        outside = self.root / "private" / "notes.txt"
        outside.parent.mkdir()
        outside.write_text("secret", encoding="utf-8")
        with patch("server.subprocess.Popen") as popen:
            for path in (outside, outside.parent, self.root, self.downloads / ".." / "private"):
                with self.subTest(path=path):
                    status, result = self.post_json("/api/reveal", {"path": str(path)})
                    self.assertEqual((status, result["status"]), (403, "outside_allowed_folders"))
            status, result = self.post_json(
                "/api/reveal", {"path": str(self.root / "private" / "missing.txt")})
            self.assertEqual((status, result["status"]), (403, "outside_allowed_folders"))
        popen.assert_not_called()

        for folder in (self.vortex, self.game, self.quarantine):
            folder.mkdir()
            archive = folder / "Мод с пробелом.zip"
            archive.write_bytes(b"mod")
            with self.subTest(folder=folder.name), patch("server.subprocess.Popen") as popen:
                status, result = self.post_json("/api/reveal", {"path": str(archive)})
                self.assertEqual((status, result["status"]), (200, "ok"))
                popen.assert_called_once_with(f'explorer /select,"{archive}"')

        with patch("server.subprocess.Popen") as popen:
            status, result = self.post_json("/api/reveal", {"path": str(self.game)})
        self.assertEqual((status, result["status"]), (200, "ok"))
        popen.assert_called_once_with(["explorer", str(self.game)])

    def test_rule_7_options_is_answered_with_405(self) -> None:
        status, body = self.request(
            "OPTIONS", "/api/clean/apply",
            headers={"Origin": "https://attacker.example",
                     "Access-Control-Request-Method": "POST"})

        self.assertEqual((status, json.loads(body)["status"]), (405, "method_not_allowed"))

    def test_dashboard_template_sends_the_token_and_has_the_placeholder(self) -> None:
        template = nexus_links.TEMPLATE_PATH.read_text(encoding="utf-8")

        self.assertEqual(template.count(dashboard_server.TOKEN_PLACEHOLDER.decode()), 1)
        self.assertIn("'X-ModAudit-Token': apiToken", template)


if __name__ == "__main__":
    unittest.main()
