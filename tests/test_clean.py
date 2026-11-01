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
