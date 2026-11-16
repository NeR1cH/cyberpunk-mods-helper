"""S11: мелкие несоответствия."""
from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import patch

from modaudit import nexus as mod_recommendations
import nexus_links

ROOT = Path(nexus_links.__file__).resolve().parent


class FrameworkMarkerTests(unittest.TestCase):
    def test_every_framework_marker_is_a_file_not_a_folder(self) -> None:
        for name, markers in nexus_links.CORE_FRAMEWORKS.items():
            for marker in markers:
                with self.subTest(framework=name, marker=marker):
                    self.assertTrue(PureWindowsPath(marker).suffix, "маркер без расширения — это папка")

    def test_leftover_folders_do_not_count_as_an_installed_framework(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            (game / "r6" / "scripts" / "EquipmentEx").mkdir(parents=True)
            (game / "red4ext" / "plugins" / "EquipmentEx").mkdir(parents=True)
            self.assertFalse(nexus_links.check_game_core(game)["Equipment-EX"])

            archive = game / "archive" / "pc" / "mod" / "EquipmentEx.archive"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"archive")
            self.assertTrue(nexus_links.check_game_core(game)["Equipment-EX"])

    def test_a_folder_named_like_a_marker_file_is_not_enough(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            (game / "archive" / "pc" / "mod" / "EquipmentEx.archive").mkdir(parents=True)

            self.assertFalse(nexus_links.check_game_core(game)["Equipment-EX"])


class StartScriptTests(unittest.TestCase):
    def test_start_script_falls_back_to_the_py_launcher_and_checks_the_version(self) -> None:
        script = (ROOT / "start.bat").read_text(encoding="utf-8")

        self.assertIn('set "PY=py -3"', script)
        self.assertIn("sys.version_info >= (3, 10)", script)
        self.assertIn("%PY% nexus_links.py scan", script)
        self.assertIn("%PY% server.py", script)
        for line in script.splitlines():
            with self.subTest(line=line):
                # Единственная прямая команда python — сама проверка, есть ли он в PATH.
                self.assertFalse(line.strip().startswith("python ")
                                 and not line.strip().startswith("python --version"),
                                 "команда должна идти через %PY%")


class AiModelListTests(unittest.TestCase):
    FREE = {"data": [{"id": "zeta/model:free"}, {"id": "nvidia/model:free"}, {"id": "paid/model"}]}

    def test_example_config_and_code_list_the_same_default_models(self) -> None:
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

        self.assertEqual(example["ai"]["models"], nexus_links.DEFAULT_AI_MODELS)

    def run_command(self, config: Path, answer: str | None, write: bool = True) -> int:
        args = Namespace(config=str(config), write_config=write)
        prompt = patch("builtins.input", side_effect=EOFError if answer is None else [answer])
        with patch("nexus_links._http_json", return_value=self.FREE), prompt as asked, \
                patch("builtins.print"):
            code = nexus_links.cmd_ai_models(args)
        self.asked = asked.call_count
        return code

    def test_write_config_changes_only_the_model_list_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.json"
            config.write_text(json.dumps({
                "downloads": "D:\\\\Моды", "ai": {"api_key": "secret", "models": ["old/model"]},
            }, ensure_ascii=False), encoding="utf-8")

            self.assertEqual(self.run_command(config, "да"), 0)

            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), {
                "downloads": "D:\\\\Моды",
                "ai": {"api_key": "secret", "models": ["nvidia/model:free", "zeta/model:free"]},
            })
            self.assertEqual([p.name for p in Path(temp_dir).iterdir()], ["config.json"])

    def test_config_is_untouched_without_confirmation_or_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.json"
            original = '{"ai": {"models": ["old/model"]}}'
            config.write_text(original, encoding="utf-8")

            for answer in ("", "n", "нет", None):
                with self.subTest(answer=answer):
                    self.assertEqual(self.run_command(config, answer), 0)
                    self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertEqual(self.run_command(config, "y", write=False), 0)
            self.assertEqual(self.asked, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_broken_config_is_reported_and_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.json"
            config.write_text("{broken", encoding="utf-8")

            with self.assertLogs("modaudit", level="ERROR"):
                self.assertEqual(self.run_command(config, "y"), 2)

            self.assertEqual(config.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(self.asked, 0)


class NexusCacheWriteTests(unittest.TestCase):
    def page(self, mod_id: int, description: str = "text") -> bytes:
        return json.dumps({"data": {"name": f"Mod {mod_id}", "description": description}}).encode()

    def fetch(self, cache: Path, pages: list, mod_ids: set[int]):
        with patch("modaudit.nexus._request_mod_page", side_effect=pages), \
                patch("modaudit.nexus.time.sleep"), \
                patch("modaudit.nexus._save_analysis_cache",
                      wraps=mod_recommendations._save_analysis_cache) as save:
            try:
                result = mod_recommendations.fetch_nexus_mod_records(mod_ids, cache, 10)
            except KeyboardInterrupt:
                result = None
        return result, save

    def test_cache_is_written_once_per_pass_not_once_per_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"

            result, save = self.fetch(cache, [self.page(1), self.page(2), self.page(3)], {1, 2, 3})

            self.assertEqual(save.call_count, 1)
            self.assertEqual(sorted(result), [1, 2, 3])
            self.assertEqual(sorted(mod_recommendations._load_analysis_cache(cache)), [1, 2, 3])

    def test_pages_fetched_before_an_interruption_are_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"

            result, save = self.fetch(cache, [self.page(1), KeyboardInterrupt()], {1, 2})

            self.assertIsNone(result)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(sorted(mod_recommendations._load_analysis_cache(cache)), [1])

    def test_nothing_is_written_when_nothing_was_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"

            with self.assertLogs("modaudit", level="WARNING"):
                _, save = self.fetch(cache, [OSError("offline")], {1})

            save.assert_not_called()
            self.assertFalse(cache.exists())

    def test_description_is_stored_only_in_the_length_the_analysis_uses(self) -> None:
        limit = mod_recommendations.MAX_ANALYSIS_DESCRIPTION_CHARS
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "cache.json"

            result, _ = self.fetch(cache, [self.page(1, "я" * (limit + 5000))], {1})

            self.assertEqual(len(result[1].description), limit)
            self.assertEqual(len(mod_recommendations._load_analysis_cache(cache)[1].description), limit)
            self.assertLess(cache.stat().st_size, 3 * limit)


if __name__ == "__main__":
    unittest.main()
