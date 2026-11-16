"""R01: код живёт в пакете modaudit, старые имена модулей остаются обёртками."""
from __future__ import annotations

import importlib
from pathlib import Path
import unittest
from unittest.mock import patch

import nexus_links

ROOT = Path(nexus_links.__file__).resolve().parent

# старое имя модуля → новый модуль и несколько имён, которые должны быть доступны по-старому
WRAPPERS = {
    "openrouter_limits": ("modaudit.limits", ("reserve_request", "configure_budgets", "progress_line")),
    "mod_recommendations": ("modaudit.nexus", ("NexusModRecord", "fetch_nexus_mod_records",
                                               "load_installation_notes")),
    "mod_categories": ("modaudit.sources", ("build_scan_plan", "summarize_sources")),
}


class PackageLayoutTests(unittest.TestCase):
    def test_old_module_names_re_export_the_new_modules(self) -> None:
        for old_name, (new_name, names) in WRAPPERS.items():
            old, new = importlib.import_module(old_name), importlib.import_module(new_name)
            for name in names:
                with self.subTest(module=old_name, name=name):
                    self.assertIs(getattr(old, name), getattr(new, name))

    def test_wrappers_contain_no_logic_of_their_own(self) -> None:
        for old_name in WRAPPERS:
            source = (ROOT / f"{old_name}.py").read_text(encoding="utf-8")
            with self.subTest(module=old_name):
                self.assertNotIn("def ", source.replace("def main", ""))
                self.assertLess(len(source.splitlines()), 15)

    def test_user_files_stay_in_the_project_folder_not_inside_the_package(self) -> None:
        from modaudit import PROJECT_DIR, limits

        self.assertEqual(PROJECT_DIR, ROOT)
        self.assertEqual(limits.USAGE_PATH, ROOT / "openrouter_usage.json")
        self.assertEqual(limits.ANALYSIS_CACHE_PATH, ROOT / "nexus_analysis_cache.json")
        self.assertEqual(limits.CONFIG_PATH, ROOT / "config.json")
        self.assertEqual(nexus_links.CONFIG_PATH, ROOT / "config.json")
        self.assertEqual(nexus_links.HASH_CACHE_PATH, ROOT / "mod_cache.json")
        self.assertEqual(nexus_links.TEMPLATE_PATH, ROOT / "dashboard_template.html")

    def test_progress_command_still_works_through_the_old_script_name(self) -> None:
        from modaudit import limits

        with patch("modaudit.limits.progress_line", return_value="7|3"), patch("builtins.print") as out:
            self.assertEqual(limits.main(["progress"]), 0)
        out.assert_called_once_with("7|3")
        with patch("modaudit.limits.progress_line", return_value="-1|-1"), patch("builtins.print"):
            self.assertEqual(limits.main(["progress"]), 1)
        self.assertIn("limits.main", (ROOT / "openrouter_limits.py").read_text(encoding="utf-8"))

    def test_moved_modules_are_not_duplicated_in_the_project_root(self) -> None:
        for name in ("json_files.py", "hash_cache.py"):
            with self.subTest(name=name):
                self.assertFalse((ROOT / name).exists())
                self.assertTrue((ROOT / "modaudit" / name).is_file())


if __name__ == "__main__":
    unittest.main()
