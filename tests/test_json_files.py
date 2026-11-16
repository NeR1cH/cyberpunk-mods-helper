from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from modaudit import nexus as mod_recommendations
import nexus_links
from modaudit import limits as openrouter_limits

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _note() -> mod_recommendations.InstallationNote:
    return mod_recommendations.InstallationNote(1234, "manual", "Распакуйте в папку игры", 10.0)


def _record() -> mod_recommendations.NexusModRecord:
    return mod_recommendations.NexusModRecord(
        mod_id=1234,
        name="Example mod",
        summary="Short summary",
        description="Full description",
        category="Gameplay",
        tags=("Gameplay",),
        fetched_at=10,
    )


# (имя, загрузчик, функция сохранения новых данных, проверка новых данных)
CACHES = (
    (
        "ai_cache",
        nexus_links.load_ai_cache,
        lambda path: nexus_links.save_ai_cache({"мод": {"en": "Mod"}}, path),
        lambda path: nexus_links.load_ai_cache(path) == {"мод": {"en": "Mod"}},
    ),
    (
        "nexus_install_cache",
        mod_recommendations._load_cache,
        lambda path: mod_recommendations._save_cache({1234: _note()}, path),
        lambda path: mod_recommendations._load_cache(path) == {1234: _note()},
    ),
    (
        "nexus_analysis_cache",
        mod_recommendations._load_analysis_cache,
        lambda path: mod_recommendations._save_analysis_cache({1234: _record()}, path),
        lambda path: mod_recommendations._load_analysis_cache(path) == {1234: _record()},
    ),
)


def _leftovers(folder: Path, keep: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p != keep)


class AtomicWriteJsonTests(unittest.TestCase):
    def test_writes_utf8_json_and_leaves_no_temporary_files(self) -> None:
        from modaudit.json_files import atomic_write_json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "папка с пробелом" / "cache.json"
            path.parent.mkdir()
            atomic_write_json(path, {"название": "Виртуальная мастерская"})

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"название": "Виртуальная мастерская"},
            )
            self.assertIn("Виртуальная", path.read_text(encoding="utf-8"))
            self.assertEqual(_leftovers(path.parent, path), [])

    def test_failed_replace_keeps_previous_file_and_removes_temporary_file(self) -> None:
        from modaudit.json_files import atomic_write_json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cache.json"
            path.write_text('{"old": true}', encoding="utf-8")

            with patch("modaudit.json_files.os.replace", side_effect=PermissionError("file is locked")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"new": True})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(_leftovers(path.parent, path), [])

    def test_unserializable_data_does_not_touch_the_file(self) -> None:
        from modaudit.json_files import atomic_write_json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cache.json"
            path.write_text('{"old": true}', encoding="utf-8")

            with self.assertRaises(TypeError):
                atomic_write_json(path, {"bad": object()})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(_leftovers(path.parent, path), [])


class CacheWriteSafetyTests(unittest.TestCase):
    def test_interrupted_save_keeps_previous_cache_intact(self) -> None:
        for name, load, save, is_saved in CACHES:
            with self.subTest(cache=name), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / f"{name}.json"
                save(path)
                before = path.read_bytes()

                # Обрыв в момент подмены файла: до этого шага настоящий кэш трогать нельзя.
                with patch("os.replace", side_effect=OSError("interrupted")):
                    with self.assertLogs("modaudit", level="WARNING"):
                        save(path)

                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(is_saved(path))
                self.assertEqual(_leftovers(path.parent, path), [])

    def test_corrupt_cache_is_preserved_and_new_data_is_written_separately(self) -> None:
        broken = '{"version": 1, "items": {"мод": {"en": "Mo'
        for name, load, save, is_saved in CACHES:
            with self.subTest(cache=name), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / f"{name}.json"
                path.write_text(broken, encoding="utf-8")

                with self.assertLogs("modaudit", level="WARNING"):
                    self.assertEqual(load(path), {})

                preserved = list(Path(temp_dir).glob(f"{name}.corrupt-*.json"))
                self.assertEqual(len(preserved), 1)
                self.assertEqual(preserved[0].read_text(encoding="utf-8"), broken)
                self.assertFalse(path.exists())

                save(path)

                self.assertTrue(is_saved(path))
                self.assertEqual(preserved[0].read_text(encoding="utf-8"), broken)
                self.assertEqual(_leftovers(path.parent, path), [preserved[0].name])

    def test_cache_with_unusable_structure_is_preserved_too(self) -> None:
        for name, load, save, is_saved in CACHES:
            for content in ("[]", '{"version": 999, "items": []}'):
                with self.subTest(cache=name, content=content), \
                        tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / f"{name}.json"
                    path.write_text(content, encoding="utf-8")

                    with self.assertLogs("modaudit", level="WARNING"):
                        self.assertEqual(load(path), {})

                    preserved = list(Path(temp_dir).glob(f"{name}.corrupt-*.json"))
                    self.assertEqual(len(preserved), 1)
                    self.assertEqual(preserved[0].read_text(encoding="utf-8"), content)

    def test_two_corrupt_caches_in_the_same_second_get_different_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ai_cache.json"
            for content in ("{first", "{second"):
                path.write_text(content, encoding="utf-8")
                with self.assertLogs("modaudit", level="WARNING"):
                    nexus_links.load_ai_cache(path)

            preserved = sorted(
                p.read_text(encoding="utf-8") for p in Path(temp_dir).glob("ai_cache.corrupt-*.json")
            )
            self.assertEqual(preserved, ["{first", "{second"])

    def test_unreadable_cache_is_not_treated_as_corrupt(self) -> None:
        for name, load, save, is_saved in CACHES:
            with self.subTest(cache=name), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / f"{name}.json"
                save(path)
                before = path.read_bytes()

                with patch.object(Path, "read_text", side_effect=PermissionError("locked")):
                    with self.assertLogs("modaudit", level="WARNING"):
                        self.assertEqual(load(path), {})

                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(_leftovers(path.parent, path), [])

    def test_usage_ledger_is_written_through_the_shared_function(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir) / "usage.json"
            with patch.object(openrouter_limits, "_requests_this_process", 0):
                with patch("modaudit.json_files.os.replace", side_effect=OSError("interrupted")):
                    with self.assertLogs("modaudit", level="ERROR"):
                        self.assertFalse(openrouter_limits.reserve_request(usage_path))

            self.assertFalse(usage_path.exists())
            self.assertEqual(
                [p.name for p in Path(temp_dir).iterdir() if p.suffix == ".tmp"], [])


class SingleJsonWriterTests(unittest.TestCase):
    def test_project_modules_do_not_write_json_files_on_their_own(self) -> None:
        direct_write = re.compile(r"write_text\(\s*json\.dumps|json\.dump\(")
        offenders = [
            source.name
            for source in sorted([*PROJECT_ROOT.glob("*.py"), *(PROJECT_ROOT / "modaudit").glob("*.py")])
            if source.name != "json_files.py"
            and direct_write.search(source.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
