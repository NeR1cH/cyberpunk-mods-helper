import tempfile
import unittest
from pathlib import Path

import nexus_links
from mod_categories import build_scan_plan, source_categories, summarize_sources
from mod_recommendations import InstallationNote


def archive(file_id: int, source: str, title: str, mod_id: int | None = None) -> nexus_links.Archive:
    return nexus_links.Archive(
        id=file_id,
        source=source,
        path=Path(f"{source}\\{title}.zip"),
        size=100,
        mtime=0,
        meta=nexus_links.ParsedName(title, mod_id),
    )


class GroupingTests(unittest.TestCase):
    def test_source_categories_only_match_vortex_exclusive_groups(self) -> None:
        self.assertEqual(source_categories(["vortex"]), {"vortex"})
        self.assertEqual(source_categories(["downloads"]), set())
        self.assertEqual(source_categories(["downloads", "vortex"]), set())
        self.assertEqual(source_categories([]), set())

    def test_source_summary_consumes_archive_and_group_iterables_once(self) -> None:
        files = [
            archive(0, "downloads", "Downloads A", 1),
            archive(1, "downloads", "Downloads B", 2),
            archive(2, "vortex", "Vortex Only", 3),
            archive(3, "downloads", "Shared", 4),
            archive(4, "vortex", "Shared", 4),
            archive(5, "other", "Other source", 5),
        ]
        groups = nexus_links.build_rows(files, {})

        summary = summarize_sources(iter(files), iter(groups))

        self.assertEqual(summary.downloads_archives, 3)
        self.assertEqual(summary.vortex_archives, 2)
        self.assertEqual(summary.downloads_only_groups, 2)
        self.assertEqual(summary.vortex_only_groups, 1)
        self.assertEqual(summary.both_source_groups, 1)

    def test_same_mod_id_in_both_sources_is_one_group(self) -> None:
        files = [
            archive(0, "downloads", "Sample Mod", 1234),
            archive(1, "vortex", "Sample Mod", 1234),
        ]

        rows = nexus_links.build_rows(files, {})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cats, {"all", "nexus_downloads"})
        self.assertEqual(len(rows[0].files), 2)
        self.assertIn("ID в имени архива", rows[0].grouping_reason)

    def test_title_only_group_can_span_both_sources(self) -> None:
        files = [
            archive(0, "downloads", "Unknown Mod"),
            archive(1, "vortex", "Unknown Mod"),
        ]

        rows = nexus_links.build_rows(files, {})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cats, {"all", "nexus_downloads"})
        self.assertEqual(rows[0].grouping_reason, "очищенное название архива (ID не распознан)")

    def test_source_filters_preserve_exclusive_categories_and_include_nexus_filter(self) -> None:
        files = [
            archive(0, "downloads", "Downloads Only", 1),
            archive(1, "vortex", "Vortex Only", 2),
            archive(2, "downloads", "Shared", 3),
            archive(3, "vortex", "Shared", 3),
        ]

        rows = {row.title: row for row in nexus_links.build_rows(files, {})}

        self.assertIn("nexus_downloads", rows["Downloads Only"].cats)
        self.assertNotIn("vortex", rows["Downloads Only"].cats)
        self.assertIn("vortex", rows["Vortex Only"].cats)
        self.assertNotIn("nexus_downloads", rows["Vortex Only"].cats)
        self.assertNotIn("vortex", rows["Shared"].cats)
        self.assertIn("nexus_downloads", rows["Shared"].cats)
        self.assertIn("способ установки не определён", rows["Downloads Only"].actions[0])

    def test_ai_mapping_can_merge_an_id_less_title_group(self) -> None:
        files = [
            archive(0, "downloads", "Unknown Mod"),
            archive(1, "vortex", "Unknown Mod", 1234),
        ]

        without_mapping = nexus_links.build_rows(files, {})
        with_mapping = nexus_links.build_rows(files, {}, {"unknown mod": 1234})

        self.assertEqual(len(without_mapping), 2)
        self.assertEqual(len(with_mapping), 1)
        self.assertIn("название ИИ совпало", with_mapping[0].grouping_reason)

    def test_source_summary_distinguishes_groups_from_archives(self) -> None:
        files = [
            archive(0, "downloads", "Downloads Only", 1),
            archive(1, "vortex", "Vortex Only", 2),
            archive(2, "downloads", "Shared", 3),
            archive(3, "vortex", "Shared", 3),
        ]
        rows = nexus_links.build_rows(files, {})

        html = nexus_links.render_dashboard({}, rows, files)

        self.assertIn("Групп модов: <strong>3</strong>", html)
        self.assertIn("архивов: <strong>4</strong>", html)
        self.assertIn("Группы только в Downloads: <strong>1</strong>", html)
        self.assertIn("только в Vortex: <strong>1</strong>", html)
        self.assertIn("в обеих папках: <strong>1</strong>", html)
        self.assertIn("Downloads: <strong>2</strong>, Vortex: <strong>2</strong>", html)
        self.assertIn("ID в имени архива", html)
        self.assertIn('data-filter="nexus_downloads" title="Все группы с архивами из downloads', html)
        self.assertIn(">Локальные в игре (0)</button>", html)
        self.assertIn(">Из Nexus Downloads (2)</button>", html)
        self.assertIn(">Моды в Vortex Downloads (1)</button>", html)
        self.assertIn("Папка игры не найдена", html)

    def test_game_mod_scan_lists_known_mod_folders_and_ignores_other_game_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            archive_dir = game / "archive" / "pc" / "mod"
            archive_dir.mkdir(parents=True)
            (archive_dir / "sample.archive").write_bytes(b"archive")
            scripts = game / "r6" / "scripts" / "SampleScript"
            scripts.mkdir(parents=True)
            (scripts / "init.reds").write_bytes(b"script")
            (game / "Cyberpunk2077.exe").write_bytes(b"game")
            (game / "untracked" / "file.dll").parent.mkdir()
            (game / "untracked" / "file.dll").write_bytes(b"not in known mod folders")

            entries = nexus_links.scan_game_mods(game)

        self.assertEqual(
            [(entry.section, entry.name, entry.file_count) for entry in entries],
            [("archive/pc/mod", "sample.archive", 1), ("r6/scripts", "SampleScript", 1)],
        )
        self.assertEqual([entry.size for entry in entries], [7, 6])

    def test_game_mod_scan_includes_files_and_ignores_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            root = game / "archive" / "pc" / "mod"
            root.mkdir(parents=True)
            (root / "standalone.archive").write_bytes(b"archive")
            (root / "empty").mkdir()

            entries = nexus_links.scan_game_mods(game)

        self.assertEqual(
            [(entry.name, entry.file_count, entry.size) for entry in entries],
            [("standalone.archive", 1, 7)],
        )

    def test_game_mod_inventory_renders_in_separate_local_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            game = Path(temp_dir)
            root = game / "mods"
            mod = root / "SampleRedmod"
            mod.mkdir(parents=True)
            (mod / "info.json").write_text("{}", encoding="utf-8")
            entries = nexus_links.scan_game_mods(game)
            html = nexus_links.render_dashboard({}, [], [], game_mods=entries, game_path=game)

        self.assertIn(">Локальные в игре (1)</button>", html)
        self.assertIn('id="gameModsContainer" hidden', html)
        self.assertIn("SampleRedmod", html)
        self.assertIn("Найдено элементов: 1", html)

    def test_scan_order_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "z.zip").write_bytes(b"z")
            (root / "A.zip").write_bytes(b"a")
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.zip").write_bytes(b"b")

            first = nexus_links.scan_folder(root, "downloads")
            second = nexus_links.scan_folder(root, "downloads")

        self.assertEqual(
            [item.path.name for item in first],
            ["A.zip", "z.zip", "b.zip"],
        )
        self.assertEqual(
            [item.path for item in first],
            [item.path for item in second],
        )

    def test_nested_source_roots_exclude_inner_root_from_outer_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "Downloads"
            vortex = downloads / "Vortex"
            vortex.mkdir(parents=True)
            (downloads / "manual.zip").write_bytes(b"manual")
            (vortex / "managed.zip").write_bytes(b"managed")

            plan = build_scan_plan(downloads, vortex)

            self.assertEqual([item.source for item in plan], ["downloads", "vortex"])
            self.assertEqual(plan[0].exclude, (vortex.resolve(),))
            self.assertEqual(plan[1].exclude, ())
            archives = [
                archive
                for item in plan
                for archive in nexus_links.scan_folder(
                    item.folder,
                    item.source,
                    exclude=item.exclude,
                )
            ]
            self.assertEqual(
                {(item.source, item.path.name) for item in archives},
                {("downloads", "manual.zip"), ("vortex", "managed.zip")},
            )

            reverse_plan = build_scan_plan(vortex, downloads)

            self.assertEqual(reverse_plan[0].exclude, ())
            self.assertEqual(reverse_plan[1].exclude, (vortex.resolve(),))

    def test_identical_source_roots_are_scanned_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            plan = build_scan_plan(root, root / ".")

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].source, "downloads")
        self.assertIn("сканирую её один раз", plan[0].warning)

    def test_disjoint_source_roots_are_scanned_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "Downloads"
            vortex = root / "Vortex"

            plan = build_scan_plan(downloads, vortex)

        self.assertEqual([item.source for item in plan], ["downloads", "vortex"])
        self.assertEqual(plan[0].exclude, ())
        self.assertEqual(plan[1].exclude, ())

    def test_author_install_note_is_optional_and_html_escaped(self) -> None:
        note = InstallationNote(
            mod_id=1234,
            method="manual",
            excerpt="Copy <script>alert(1)</script> into the game folder.",
            checked_at=0,
        )

        rendered = nexus_links.render_installation_note(note)

        self.assertIn("автор описывает ручную установку", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("nexusmods.com/cyberpunk2077/mods/1234", rendered)

        row = nexus_links.build_rows(
            [archive(0, "downloads", "Sample Mod", 1234)],
            {},
        )[0]
        rendered_row = nexus_links.render_row(
            row,
            {0: 0},
            installation_notes={1234: note},
        )
        self.assertIn("По описанию автора", rendered_row)
        self.assertIn("&lt;script&gt;", rendered_row)


if __name__ == "__main__":
    unittest.main()
