from argparse import Namespace
import json
from pathlib import Path
import re
import tempfile
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

        # Счётчик, перенос в карантин и экспорт selection.json берут файлы из одного места.
        sources = re.findall(r"const chosen = ([^;]+);", page)

        self.assertEqual(sources, ["selectedFiles()"] * 3)
        self.assertEqual(page.count("buildSelection(chosen, "), 2)
        self.assertIn("selectedIds.has(f.fid)", page)
        self.assertNotIn("fileData.filter(f => f.s ===", page)

    def test_selection_counter_and_bulk_buttons_are_rendered(self) -> None:
        page = _render_sample_dashboard()

        for element_id in ("selectionPill", "btnSelectDups", "btnSelectOld", "btnClearSelection",
                           "btnCleanSelected", "btnExportJson", "cleanDialog", "btnCleanApply"):
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



def _render_with_duplicates(
    keeper_sources: list[str],
    downloads: Path | None,
    vortex: Path | None,
) -> str:
    archives: list[nexus_links.Archive] = []
    for source in keeper_sources:
        keeper = _archive(len(archives), source, f"keeper-{len(archives)}.zip")
        archives.append(keeper)
        archives.append(_archive(
            len(archives), "downloads", f"copy-{len(archives)}.zip",
            nexus_links.EXACT_DUP, keeper=keeper.id,
        ))
    row = nexus_links.ModRow(
        mod_id=1234,
        title="Sample Mod",
        files=archives,
        danger="",
        status="dup",
        actions=["Удалить точную копию"],
        cats={"all", "dup"},
        grouping_reason="same mod ID",
    )
    with patch("nexus_links.load_custom_blacklist", return_value={}):
        return nexus_links.render_dashboard(
            {}, [row] if archives else [], archives, downloads=downloads, vortex=vortex)


def _banner_text(page: str) -> str:
    return re.search(r'<div class="dup-banner-text">(.*?)</div>', page, re.DOTALL).group(1)


class SourceLabelTests(unittest.TestCase):
    """S03: подписи дисков и текст баннера берутся из настроек и из фактического keeper."""

    HARDCODED = ("диск C:", "диск D:", "диске C:", "(C:)", "(D:)", "DOWNLOADS C:", "VORTEX D:")

    def test_downloads_on_another_drive_without_vortex(self) -> None:
        page = _render_with_duplicates(["downloads"], Path("E:\\Моды\\Загрузки"), None)
        banner = _banner_text(page)

        for text in self.HARDCODED:
            with self.subTest(text=text):
                self.assertNotIn(text, page)
        self.assertIn("В Downloads (E:)<", page)
        self.assertIn("В Vortex (не задан)<", page)
        self.assertIn('data-downloads-label="Downloads (E:)"', page)
        self.assertIn('data-vortex-label="Vortex (не задан)"', page)
        self.assertIn("в самой папке Downloads (E:)", banner)
        self.assertNotIn("в Vortex", banner)
        self.assertNotIn("D:", banner)

    def test_banner_names_vortex_only_when_vortex_keeps_the_copy(self) -> None:
        page = _render_with_duplicates(["vortex"], Path("c:\\Users\\Ник\\Downloads"), Path("F:\\Vortex"))
        banner = _banner_text(page)

        self.assertIn("в Vortex (F:)", banner)
        self.assertIn("из Downloads (C:)", banner)
        self.assertNotIn("в самой папке", banner)

    def test_banner_counts_copies_kept_in_each_source(self) -> None:
        page = _render_with_duplicates(
            ["vortex", "downloads", "downloads"], Path("E:\\dl"), Path("D:\\vx"))
        banner = _banner_text(page)

        self.assertIn("в Vortex (D:) — 1", banner)
        self.assertIn("в самой папке Downloads (E:) — 2", banner)

    def test_banner_without_exact_copies_does_not_promise_anything(self) -> None:
        banner = _banner_text(_render_with_duplicates([], Path("E:\\dl"), Path("D:\\vx")))

        self.assertIn("Точных копий не найдено", banner)

    def test_paths_without_drive_letter_get_plain_labels(self) -> None:
        page = _render_with_duplicates(["downloads"], Path("/home/user/Загрузки"), Path("/mnt/vortex"))

        self.assertIn("В Downloads<", page)
        self.assertIn("В Vortex<", page)
        self.assertIn("в самой папке Downloads;", _banner_text(page))

    def test_labels_are_html_escaped(self) -> None:
        page = _render_with_duplicates(["downloads"], Path('\\\\nas\\"<b>\\dl'), None)

        self.assertNotIn('"<b>', page)
        self.assertIn("&lt;b&gt;", page)

    def test_template_has_no_hardcoded_drive_letters(self) -> None:
        template = nexus_links.TEMPLATE_PATH.read_text(encoding="utf-8")

        for text in self.HARDCODED:
            with self.subTest(text=text):
                self.assertNotIn(text, template)

    def test_cmd_scan_passes_configured_folders_to_the_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "Downloads"
            downloads.mkdir()
            settings = nexus_links.Settings(
                downloads=downloads,
                vortex=None,
                game=None,
                out=root / "dashboard.html",
                quarantine=root / "quarantine",
            )
            args = Namespace(
                ai=False, ai_all=False, ai_auto=False, nexus_analyze=False,
                nexus_details=False, nexus_max_requests=0, no_open=True,
            )
            with (
                patch("nexus_links.load_settings", return_value=settings),
                patch("nexus_links.load_blacklist", return_value={}),
                patch("nexus_links.check_game_core", return_value={}),
                patch("nexus_links.scan_game_mods", return_value=[]),
                patch("nexus_links.run_ai", return_value={}),
                patch("nexus_links.render_dashboard", return_value="Дашборд") as render,
            ):
                self.assertEqual(nexus_links.cmd_scan(args), 0)

        self.assertEqual(render.call_args.kwargs["downloads"], downloads)
        self.assertIsNone(render.call_args.kwargs["vortex"])

if __name__ == "__main__":
    unittest.main()
