import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import nexus_links


def _archive(
    file_id: int,
    source: str,
    name: str,
    verdict: str = nexus_links.KEEP,
    keeper: int | None = None,
) -> nexus_links.Archive:
    return nexus_links.Archive(
        id=file_id,
        source=source,
        path=Path(source) / name,
        size=1024 * (file_id + 1),
        mtime=1,
        meta=nexus_links.ParsedName("Sample Mod", 1234),
        sha256="ab" * 32 if verdict == nexus_links.EXACT_DUP else "",
        verdict=verdict,
        keeper=keeper,
    )


def _render_sample_dashboard() -> str:
    original = _archive(0, "downloads", "sample.zip")
    exact_copy = _archive(1, "downloads", "sample (1).zip", nexus_links.EXACT_DUP, keeper=0)
    old_version = _archive(2, "downloads", "sample-old.zip", nexus_links.OLDER, keeper=3)
    managed = _archive(3, "vortex", "sample-new.zip")
    row = nexus_links.ModRow(
        mod_id=1234,
        title="Sample Mod",
        files=[managed, old_version, exact_copy, original],
        danger="",
        status="dup",
        actions=["Удалить точную копию"],
        cats={"all", "dup", "old"},
        grouping_reason="same mod ID",
    )
    archives = [original, exact_copy, old_version, managed]
    with patch("nexus_links.load_custom_blacklist", return_value={}):
        return nexus_links.render_dashboard({}, [row], archives)


class SelectionContractTests(unittest.TestCase):
    """S01: файлы для очистки берутся из отмеченных галочек, а не из вердикта."""

    def test_checkboxes_point_at_candidates_and_only_exact_copies_are_preselected(self) -> None:
        page = _render_sample_dashboard()
        records = json.loads(
            re.search(r'<script id="modAuditData" type="application/json">(.*?)</script>',
                      page, re.DOTALL).group(1))
        boxes = re.findall(r'<input type="checkbox" class="sel" data-fid="(\d+)"( checked)? ', page)

        self.assertEqual(len(boxes), 2)
        for fid, checked in boxes:
            record = records[int(fid)]
            self.assertEqual(record["s"], "downloads")
            self.assertIn(record["vd"], (nexus_links.EXACT_DUP, nexus_links.OLDER))
            self.assertEqual(bool(checked), record["vd"] == nexus_links.EXACT_DUP)

    def test_cleanup_actions_take_files_from_the_checkbox_selection(self) -> None:
        page = _render_sample_dashboard()

        # Три действия очистки: прямое удаление, BAT и экспорт selection.json.
        sources = re.findall(r"const doomed = ([^;]+);", page)

        self.assertEqual(sorted(sources), ["selectedDups()", "selectedDups()", "selectedFiles()"])
        self.assertIn("selectedIds.has(f.fid)", page)

    def test_selection_counter_and_bulk_buttons_are_rendered(self) -> None:
        page = _render_sample_dashboard()

        for element_id in ("selectionPill", "btnSelectDups", "btnSelectOld", "btnClearSelection"):
            with self.subTest(element_id=element_id):
                self.assertEqual(page.count(f'id="{element_id}"'), 1)


class DashboardDataTests(unittest.TestCase):
    def test_file_payload_preserves_display_order_keepers_and_script_escaping(self) -> None:
        candidate = nexus_links.Archive(
            id=1,
            source="downloads",
            path=Path("unsafe.zip"),
            size=10,
            mtime=1,
            meta=nexus_links.ParsedName("</script>", 1234),
            verdict=nexus_links.EXACT_DUP,
            keeper=2,
        )
        keeper = nexus_links.Archive(
            id=2,
            source="vortex",
            path=Path("keeper.zip"),
            size=10,
            mtime=1,
            meta=nexus_links.ParsedName("Unsafe Mod", 1234),
        )
        row = nexus_links.ModRow(
            mod_id=1234,
            title="Unsafe Mod",
            files=[keeper, candidate],
            danger="",
            status="dup",
            actions=["Keep Vortex copy"],
            cats={"all", "dup"},
            grouping_reason="same mod ID",
        )

        index_of, payload = nexus_links._dashboard_file_data([row], [candidate, keeper])
        records = json.loads(payload)

        self.assertEqual(index_of, {2: 0, 1: 1})
        self.assertEqual([record["n"] for record in records], ["keeper.zip", "unsafe.zip"])
        self.assertEqual(records[1]["t"], "</script>")
        self.assertEqual(records[1]["k"], str(keeper.path))
        self.assertNotIn("</script>", payload.lower())



if __name__ == "__main__":
    unittest.main()
