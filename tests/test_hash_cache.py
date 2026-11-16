"""S07: кэш хешей и один процесс вместо цикла в start.bat."""
from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hash_cache import HashCache
import nexus_links
import openrouter_limits

PROJECT_ROOT = Path(nexus_links.__file__).resolve().parent
SHA = "ab" * 32
MD5 = "cd" * 16


class HashCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "mod_cache.json"

    def test_entry_is_returned_only_for_the_same_size_and_mtime(self) -> None:
        cache = HashCache(self.path)
        archive = self.root / "dl" / "mod.zip"
        cache.put(archive, 100, 5_000, SHA, MD5)

        self.assertEqual(cache.get(archive, 100, 5_000), {
            "size": 100, "mtime_ns": 5_000, "sha256": SHA, "md5": MD5})
        self.assertIsNone(cache.get(archive, 101, 5_000))
        self.assertIsNone(cache.get(archive, 100, 5_001))
        self.assertIsNone(cache.get(self.root / "dl" / "other.zip", 100, 5_000))

    def test_cache_survives_a_save_and_load_and_is_written_atomically(self) -> None:
        cache = HashCache(self.path)
        archive = self.root / "Загрузки модов" / "Мод (1).zip"
        cache.put(archive, 100, 5_000, SHA, MD5)
        cache.save()

        restored = HashCache.load(self.path)

        self.assertEqual(restored.get(archive, 100, 5_000)["sha256"], SHA)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["version"], 1)
        self.assertEqual([p.name for p in self.root.iterdir()], ["mod_cache.json"])

    def test_unchanged_cache_is_not_rewritten(self) -> None:
        cache = HashCache(self.path)
        with patch("hash_cache.atomic_write_json") as write:
            cache.save()
            cache.put(self.root / "mod.zip", 1, 2, SHA, MD5)
            cache.save()
            cache.save()

        self.assertEqual(write.call_count, 1)

    def test_unusable_cache_file_means_an_empty_cache(self) -> None:
        for content in ("{broken", "[]", '{"version": 99, "items": {}}',
                        '{"version": 1, "items": {"x": {"size": "1"}}}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                if "size" in content:
                    cache = HashCache.load(self.path)
                else:
                    with self.assertLogs("modaudit", level="WARNING"):
                        cache = HashCache.load(self.path)
                self.assertEqual(cache.items, {})

        self.assertEqual(HashCache.load(self.root / "missing.json").items, {})

    def test_prune_drops_vanished_files_only_inside_scanned_folders(self) -> None:
        cache = HashCache(self.path)
        downloads = self.root / "downloads"
        vortex = self.root / "vortex"
        kept = downloads / "kept.zip"
        gone = downloads / "sub" / "gone.zip"
        unplugged = vortex / "on-unplugged-drive.zip"
        lookalike = self.root / "downloads-old" / "mod.zip"
        for archive in (kept, gone, unplugged, lookalike):
            cache.put(archive, 1, 2, SHA, MD5)
        cache.dirty = False

        removed = cache.prune(scanned_roots=[downloads], seen=[kept])

        self.assertEqual(removed, 1)
        self.assertTrue(cache.dirty)
        self.assertIsNotNone(cache.get(kept, 1, 2))
        self.assertIsNone(cache.get(gone, 1, 2))
        self.assertIsNotNone(cache.get(unplugged, 1, 2))
        self.assertIsNotNone(cache.get(lookalike, 1, 2))


class ScanUsesHashCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.downloads = self.root / "Загрузки модов"
        self.downloads.mkdir()
        self.cache_path = self.root / "mod_cache.json"
        for name, data in (("a.zip", b"same"), ("a (1).zip", b"same"), ("b.zip", b"diff"),
                           ("lonely.zip", b"unique size")):
            (self.downloads / name).write_bytes(data)

    def scan(self) -> list[nexus_links.Archive]:
        archives, _ = nexus_links._scan_archive_roots(self.downloads, None)
        cache = HashCache.load(self.cache_path)
        nexus_links.mark_exact_duplicates(archives, cache)
        cache.prune([Path(os.path.realpath(self.downloads))], [a.path for a in archives])
        cache.save()
        return archives

    def test_digests_are_computed_in_one_pass_and_match_hashlib(self) -> None:
        archive = self.downloads / "a.zip"

        sha256, md5 = nexus_links.file_digests(archive)

        self.assertEqual(sha256, hashlib.sha256(b"same").hexdigest())
        self.assertEqual(md5, hashlib.md5(b"same").hexdigest())
        self.assertEqual(sha256, nexus_links.sha256_file(archive))

    def test_second_scan_of_an_unchanged_folder_reads_no_files(self) -> None:
        first = self.scan()

        with patch("nexus_links.file_digests") as digests, \
                patch("nexus_links.sha256_file") as sha256:
            second = self.scan()

        digests.assert_not_called()
        sha256.assert_not_called()
        self.assertEqual([(a.name, a.sha256, a.verdict) for a in second],
                         [(a.name, a.sha256, a.verdict) for a in first])
        self.assertEqual([a.name for a in second if a.verdict == nexus_links.EXACT_DUP],
                         ["a (1).zip"])

    def test_only_files_with_a_same_size_peer_are_hashed_and_cached(self) -> None:
        self.scan()

        items = json.loads(self.cache_path.read_text(encoding="utf-8"))["items"]

        self.assertEqual(sorted(Path(key).name for key in items), ["a (1).zip", "a.zip", "b.zip"])
        self.assertEqual({len(entry["md5"]) for entry in items.values()}, {32})

    def test_changed_file_is_hashed_again_and_others_are_reused(self) -> None:
        self.scan()
        changed = self.downloads / "b.zip"
        changed.write_bytes(b"same")
        stat = changed.stat()
        os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

        with patch("nexus_links.file_digests", wraps=nexus_links.file_digests) as digests:
            archives = self.scan()

        self.assertEqual([Path(call.args[0]).name for call in digests.call_args_list], ["b.zip"])
        self.assertEqual(sorted(a.name for a in archives if a.verdict == nexus_links.EXACT_DUP),
                         ["a (1).zip", "b.zip"])

    def test_deleted_file_leaves_the_cache(self) -> None:
        self.scan()
        (self.downloads / "b.zip").unlink()

        self.scan()

        items = json.loads(self.cache_path.read_text(encoding="utf-8"))["items"]
        self.assertEqual(sorted(Path(key).name for key in items), ["a (1).zip", "a.zip"])

    def test_file_changed_while_being_hashed_is_not_cached(self) -> None:
        target = self.downloads / "a.zip"
        real_digests = nexus_links.file_digests

        def digests_then_touch(path: Path) -> tuple[str, str]:
            result = real_digests(path)
            if Path(path) == target:
                stat = target.stat()
                os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
            return result

        with patch("nexus_links.file_digests", side_effect=digests_then_touch):
            self.scan()

        items = json.loads(self.cache_path.read_text(encoding="utf-8"))["items"]
        self.assertEqual(sorted(Path(key).name for key in items), ["a (1).zip", "b.zip"])

    def test_scan_without_a_cache_still_works(self) -> None:
        archives, _ = nexus_links._scan_archive_roots(self.downloads, None)

        nexus_links.mark_exact_duplicates(archives)

        self.assertEqual([a.name for a in archives if a.verdict == nexus_links.EXACT_DUP],
                         ["a (1).zip"])
        self.assertFalse(self.cache_path.exists())

    def test_cmd_scan_loads_and_saves_the_cache_at_the_configured_path(self) -> None:
        settings = nexus_links.Settings(self.downloads, None, None, self.root / "out.html",
                                        self.root / "quarantine")
        args = Namespace(ai=False, ai_all=False, ai_auto=False, ai_until_budget=False,
                         nexus_analyze=False, nexus_details=False, nexus_max_requests=0,
                         no_open=True)
        with (
            patch("nexus_links.load_settings", return_value=settings),
            patch("nexus_links.HASH_CACHE_PATH", self.cache_path),
            patch("nexus_links.load_blacklist", return_value={}),
            patch("nexus_links.check_game_core", return_value={}),
            patch("nexus_links.scan_game_mods", return_value=[]),
            patch("nexus_links.run_ai", return_value={}),
            patch("nexus_links.render_dashboard", return_value="Дашборд"),
        ):
            self.assertEqual(nexus_links.cmd_scan(args), 0)
            with patch("nexus_links.file_digests") as digests:
                self.assertEqual(nexus_links.cmd_scan(args), 0)

        digests.assert_not_called()
        self.assertTrue(self.cache_path.is_file())


class UntilBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(openrouter_limits.configure_budgets)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        downloads = self.root / "downloads"
        downloads.mkdir()
        (downloads / "Some Mod-1234-1-0.zip").write_bytes(b"archive")
        self.settings = nexus_links.Settings(downloads, None, None, self.root / "out.html",
                                             self.root / "quarantine")

    def run_scan(self, ai_results: list[dict], nexus_results: list[dict],
                 until_budget: bool = True, remaining: list[int | None] | None = None,
                 key: str = "key"):
        args = Namespace(ai=True, ai_all=False, ai_auto=False, ai_until_budget=until_budget,
                         nexus_analyze=True, nexus_details=False, nexus_max_requests=20,
                         no_open=True)
        with (
            patch("nexus_links.load_settings", return_value=self.settings),
            patch("nexus_links.HASH_CACHE_PATH", self.root / "mod_cache.json"),
            patch("nexus_links.load_blacklist", return_value={}),
            patch("nexus_links.check_game_core", return_value={}),
            patch("nexus_links.scan_game_mods", return_value=[]) as game_scan,
            patch("nexus_links.scan_folder", wraps=nexus_links.scan_folder) as folder_scan,
            patch("nexus_links.apply_ai_budgets", return_value=""),
            patch("nexus_links.resolve_openrouter", return_value=(key, ["test/model"])),
            patch("nexus_links.remaining_daily_requests",
                  side_effect=remaining or (lambda: 10)),
            patch("nexus_links.run_ai", side_effect=ai_results) as run_ai,
            patch("nexus_links.run_nexus_ai_analysis", side_effect=nexus_results) as analyze,
            patch("nexus_links.render_dashboard", return_value="Дашборд") as render,
        ):
            result = nexus_links.cmd_scan(args)
        self.assertEqual(result, 0)
        return run_ai, analyze, render, folder_scan, game_scan

    def test_rounds_repeat_until_nothing_changes_and_the_report_is_rendered_once(self) -> None:
        info = nexus_links.AIInfo(en="Some Mod", conf="high")
        ai_results = [{"a": info}, {"a": info, "b": info}, {"a": info, "b": info}]
        nexus_results = [{1: object()}, {1: object(), 2: object()}, {1: object(), 2: object()}]

        run_ai, analyze, render, folder_scan, game_scan = self.run_scan(ai_results, nexus_results)

        self.assertEqual((run_ai.call_count, analyze.call_count), (3, 3))
        self.assertEqual((render.call_count, folder_scan.call_count, game_scan.call_count), (1, 1, 1))
        self.assertEqual(len(render.call_args.args[3]), 2)
        self.assertEqual(len(render.call_args.args[5]), 2)

    def test_without_the_flag_there_is_exactly_one_round(self) -> None:
        info = nexus_links.AIInfo(en="Some Mod", conf="high")

        run_ai, analyze, render, _, _ = self.run_scan(
            [{"a": info}], [{1: object()}], until_budget=False)

        self.assertEqual((run_ai.call_count, analyze.call_count, render.call_count), (1, 1, 1))

    def test_without_a_key_there_is_exactly_one_round(self) -> None:
        run_ai, analyze, render, _, _ = self.run_scan([{}], [{}], key="")

        self.assertEqual((run_ai.call_count, analyze.call_count, render.call_count), (1, 1, 1))

    def test_rounds_stop_when_the_daily_budget_is_spent(self) -> None:
        info = nexus_links.AIInfo(en="Some Mod", conf="high")
        ai_results = [{"a": info}, {"a": info, "b": info}, {"a": info, "b": info, "c": info}]
        nexus_results = [{}, {}, {}]

        run_ai, _, render, _, _ = self.run_scan(
            ai_results, nexus_results, remaining=[5, 0])

        self.assertEqual((run_ai.call_count, render.call_count), (2, 1))

    def test_rounds_stop_when_the_request_counter_cannot_be_read(self) -> None:
        info = nexus_links.AIInfo(en="Some Mod", conf="high")

        run_ai, _, _, _, _ = self.run_scan(
            [{"a": info}, {"a": info, "b": info}], [{}, {}], remaining=[None])

        self.assertEqual(run_ai.call_count, 1)

    def test_runaway_progress_is_capped(self) -> None:
        info = nexus_links.AIInfo(en="Some Mod", conf="high")
        growing = [{str(n): info for n in range(count)} for count in range(1, 200)]

        with patch("nexus_links.MAX_AI_ROUNDS", 4):
            run_ai, _, render, _, _ = self.run_scan(growing, [{}] * 200)

        self.assertEqual((run_ai.call_count, render.call_count), (4, 1))


class SingleProcessStartScriptTests(unittest.TestCase):
    def test_start_script_runs_one_scan_and_has_no_loop(self) -> None:
        script = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

        self.assertEqual(script.count("nexus_links.py scan"), 1)
        self.assertIn("--ai-until-budget", script)
        for leftover in ("scan_loop", "goto", "openrouter_limits.py progress"):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, script)

    def test_progress_command_is_kept_for_compatibility(self) -> None:
        self.assertTrue(callable(openrouter_limits.progress_line))

    def test_per_run_budget_defaults_to_the_daily_budget(self) -> None:
        self.assertEqual(openrouter_limits.budgets_from_config({}), (10, 10))
        self.assertEqual(openrouter_limits.budgets_from_config({"daily_budget": 30}), (30, 30))
        self.assertEqual(
            openrouter_limits.budgets_from_config({"daily_budget": 30, "per_run_budget": 4}),
            (30, 4),
        )
        openrouter_limits.configure_budgets(daily=7)
        self.addCleanup(openrouter_limits.configure_budgets)
        self.assertEqual(openrouter_limits.current_budgets(), (7, 7))


if __name__ == "__main__":
    unittest.main()
