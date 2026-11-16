from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nexus_links


class CleanCommandTests(unittest.TestCase):
    def create_selection(self, root: Path) -> tuple[Path, Path, Path, nexus_links.Settings]:
        downloads = root / "downloads"
        downloads.mkdir()
        candidate = downloads / "candidate.zip"
        keeper = downloads / "keeper.zip"
        candidate.write_bytes(b"identical archive")
        keeper.write_bytes(b"identical archive")
        stat = candidate.stat()
        selection = root / "selection.json"
        selection.write_text(
            json.dumps({
                "files": [{
                    "path": str(candidate),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "sha256": nexus_links.sha256_file(candidate),
                    "reason": nexus_links.EXACT_DUP,
                    "keeper": str(keeper),
                }]
            }),
            encoding="utf-8",
        )
        settings = nexus_links.Settings(
            downloads=downloads,
            vortex=None,
            game=None,
            out=root / "report.html",
            quarantine=root / "quarantine",
        )
        return selection, candidate, root / "quarantine", settings

    def test_clean_dry_run_does_not_mutate_files_or_create_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            selection, candidate, quarantine, settings = self.create_selection(Path(temp_dir))
            args = Namespace(
                selection=str(selection),
                no_verify=False,
                apply=False,
                mode="quarantine",
            )

            with patch("nexus_links.load_settings", return_value=settings):
                result = nexus_links.cmd_clean(args)

            self.assertEqual(result, 0)
            self.assertTrue(candidate.is_file())
            self.assertFalse(quarantine.exists())

    def test_clean_apply_moves_validated_file_to_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            selection, candidate, quarantine, settings = self.create_selection(Path(temp_dir))
            args = Namespace(
                selection=str(selection),
                no_verify=False,
                apply=True,
                mode="quarantine",
            )

            with patch("nexus_links.load_settings", return_value=settings):
                result = nexus_links.cmd_clean(args)

            self.assertEqual(result, 0)
            self.assertFalse(candidate.exists())
            moved_files = list(quarantine.rglob("candidate.zip"))
            self.assertEqual(len(moved_files), 1)
            self.assertEqual(moved_files[0].read_bytes(), b"identical archive")

    def test_clean_apply_failure_logs_failure_and_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            selection, candidate, quarantine, settings = self.create_selection(Path(temp_dir))
            args = Namespace(
                selection=str(selection),
                no_verify=False,
                apply=True,
                mode="quarantine",
            )

            with patch("nexus_links.load_settings", return_value=settings):
                with patch("nexus_links.shutil.move", side_effect=OSError("disk full")):
                    with self.assertLogs("modaudit", level="ERROR") as captured:
                        result = nexus_links.cmd_clean(args)

            self.assertEqual(result, 1)
            self.assertTrue(candidate.is_file())
            self.assertTrue(any("disk full" in message for message in captured.output))
            log_files = list(quarantine.rglob("clean_*.log"))
            self.assertEqual(len(log_files), 1)
            self.assertIn("FAIL\t", log_files[0].read_text(encoding="utf-8"))


class CleanPlanTests(unittest.TestCase):
    """S02: части команды clean, которыми пользуется и сервер дашборда."""

    def make_entry(self, downloads: Path, name: str, data: bytes = b"identical archive") -> dict:
        candidate = downloads / name
        keeper = downloads / "keeper.zip"
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

    def test_checks_keep_entry_order_and_report_the_reason_for_each_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir) / "downloads"
            downloads.mkdir()
            good = self.make_entry(downloads, "good.zip")
            changed = dict(self.make_entry(downloads, "changed.zip"), size=1)
            entries = [changed, good, "not an object"]

            checks = list(nexus_links.iter_clean_checks(
                entries, downloads, Path(temp_dir) / "quarantine"))

        self.assertEqual([entry for entry, _ in checks], entries)
        self.assertEqual(
            [problem for _, problem in checks],
            ["файл изменился после сканирования", None, "неверный формат записи"],
        )

    def test_apply_reports_where_each_file_went(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir) / "downloads"
            downloads.mkdir()
            quarantine = Path(temp_dir) / "quarantine"
            entry = self.make_entry(downloads, "copy.zip")
            outcome = nexus_links.CleanOutcome()

            result = nexus_links._apply_clean_plan([entry], quarantine, "quarantine", outcome)

            self.assertEqual(result, 0)
            self.assertEqual(outcome.failed, [])
            self.assertEqual(len(outcome.moved), 1)
            source, destination = outcome.moved[0]
            self.assertEqual(source, entry["path"])
            self.assertEqual(Path(destination).read_bytes(), b"identical archive")
            self.assertEqual(Path(destination).parent, outcome.run_dir)
            self.assertIn(f"MOVE\t{source}\t{destination}",
                          outcome.log_path.read_text(encoding="utf-8"))

    def test_two_runs_in_the_same_second_do_not_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir) / "downloads"
            downloads.mkdir()
            quarantine = Path(temp_dir) / "quarantine"
            first = nexus_links.CleanOutcome()
            second = nexus_links.CleanOutcome()

            with patch("nexus_links.datetime") as clock:
                clock.now.return_value.strftime.return_value = "20261116_120000"
                nexus_links._apply_clean_plan(
                    [self.make_entry(downloads, "one.zip")], quarantine, "quarantine", first)
                nexus_links._apply_clean_plan(
                    [self.make_entry(downloads, "two.zip")], quarantine, "quarantine", second)

            self.assertNotEqual(first.run_dir, second.run_dir)
            self.assertNotEqual(first.log_path, second.log_path)
            self.assertIn("one.zip", first.log_path.read_text(encoding="utf-8"))
            self.assertIn("two.zip", second.log_path.read_text(encoding="utf-8"))
