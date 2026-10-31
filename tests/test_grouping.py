import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def test_archive_root_scan_preserves_order_ids_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "Downloads"
            vortex = root / "Vortex"
            downloads.mkdir()
            vortex.mkdir()
            download_archive = archive(0, "downloads", "Download Mod", 1)
            vortex_archive = archive(1, "vortex", "Vortex Mod", 2)

            with patch(
                "nexus_links.scan_folder",
                side_effect=[[download_archive], [vortex_archive]],
            ) as scan_folder:
                found, scanned_count = nexus_links._scan_archive_roots(downloads, vortex)

        self.assertEqual(found, [download_archive, vortex_archive])
        self.assertEqual(scanned_count, 2)
        calls = scan_folder.call_args_list
        self.assertEqual(calls[0].args, (downloads.resolve(), "downloads"))
        self.assertEqual(calls[0].kwargs["start_id"], 0)
        self.assertEqual(calls[0].kwargs["exclude"], ())
        self.assertEqual(calls[1].args, (vortex.resolve(), "vortex"))
        self.assertEqual(calls[1].kwargs["start_id"], 1)
        self.assertEqual(calls[1].kwargs["exclude"], ())

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

    def test_group_classification_preserves_status_priority_and_all_actions(self) -> None:
        exact = archive(0, "downloads", "Exact copy", 1234)
        exact.verdict = nexus_links.EXACT_DUP
        older = archive(1, "downloads", "Old version", 1234)
        older.verdict = nexus_links.OLDER
        newer = archive(2, "downloads", "New version", 1234)
        newer.verdict = nexus_links.NEWER
        vortex = archive(3, "vortex", "Installed version", 1234)

        status, actions, categories = nexus_links._classify_group_files(
            [exact, older, newer, vortex],
            1234,
            "Known issue",
        )

        self.assertEqual(status, "danger")
        self.assertEqual(
            categories,
            {"all", "danger", "dup", "old", "update", "nexus_downloads"},
        )
        self.assertEqual(len(actions), 3)
        self.assertIn("Точных копий в Downloads: 1", actions[0])
        self.assertIn("Старых версий в Downloads: 1", actions[1])
        self.assertIn("версия новее, чем в Vortex", actions[2])

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

    def test_exact_duplicates_prefer_vortex_keeper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                name: root / name
                for name in ("download.zip", "vortex-z.zip", "vortex-a.zip")
            }
            for path in paths.values():
                path.write_bytes(b"identical archive")
            archives = [
                nexus_links.Archive(
                    id=index,
                    source=source,
                    path=paths[name],
                    size=paths[name].stat().st_size,
                    mtime=0,
                    meta=nexus_links.ParsedName(name, None),
                )
                for index, (source, name) in enumerate((
                    ("downloads", "download.zip"),
                    ("vortex", "vortex-z.zip"),
                    ("vortex", "vortex-a.zip"),
                ))
            ]

            nexus_links.mark_exact_duplicates(archives)

        self.assertEqual(archives[0].verdict, nexus_links.EXACT_DUP)
        self.assertEqual(archives[0].keeper, archives[2].id)
        self.assertIn("в Vortex", archives[0].note)
        self.assertEqual(archives[1].verdict, nexus_links.KEEP)
        self.assertEqual(archives[2].verdict, nexus_links.KEEP)

    def test_exact_duplicates_without_vortex_keep_original_over_browser_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "z-original.zip"
            browser_copy = root / "a-copy.zip"
            original.write_bytes(b"identical archive")
            browser_copy.write_bytes(b"identical archive")
            archives = [
                nexus_links.Archive(
                    id=0,
                    source="downloads",
                    path=browser_copy,
                    size=browser_copy.stat().st_size,
                    mtime=0,
                    meta=nexus_links.ParsedName("Mod", None, copy_index=2),
                ),
                nexus_links.Archive(
                    id=1,
                    source="downloads",
                    path=original,
                    size=original.stat().st_size,
                    mtime=0,
                    meta=nexus_links.ParsedName("Mod", None),
                ),
            ]

            nexus_links.mark_exact_duplicates(archives)

        self.assertEqual(archives[0].verdict, nexus_links.EXACT_DUP)
        self.assertEqual(archives[0].keeper, archives[1].id)
        self.assertEqual(archives[1].verdict, nexus_links.KEEP)
        self.assertIn("в Downloads", archives[0].note)

    def test_version_scan_marks_older_download_and_chooses_newer_keeper(self) -> None:
        older = archive(0, "downloads", "Sample Mod", 1234)
        older.meta = nexus_links.ParsedName("Sample Mod", 1234, version="1.0", ts=100)
        newer = archive(1, "downloads", "Sample Mod", 1234)
        newer.meta = nexus_links.ParsedName("Sample Mod", 1234, version="2.0", ts=200)

        nexus_links.mark_versions([older, newer])

        self.assertEqual(older.verdict, nexus_links.OLDER)
        self.assertEqual(older.keeper, newer.id)
        self.assertIn(newer.name, older.note)
        self.assertEqual(newer.verdict, nexus_links.KEEP)

    def test_version_scan_marks_download_newer_than_vortex(self) -> None:
        downloaded = archive(0, "downloads", "Sample Mod", 1234)
        downloaded.meta = nexus_links.ParsedName(
            "Sample Mod",
            1234,
            version="2.0",
            ts=200,
        )
        installed_archive = archive(1, "vortex", "Sample Mod", 1234)
        installed_archive.meta = nexus_links.ParsedName(
            "Sample Mod",
            1234,
            version="1.0",
            ts=100,
        )

        nexus_links.mark_versions([downloaded, installed_archive])

        self.assertEqual(downloaded.verdict, nexus_links.NEWER)
        self.assertEqual(installed_archive.verdict, nexus_links.STALE)
        self.assertIn("обновите мод в Vortex", downloaded.note)
        self.assertIn(downloaded.name, installed_archive.note)

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

    def test_steam_game_lookup_checks_libraries_from_vdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            steam_root = root / "Steam"
            secondary_library = root / "Library"
            game = secondary_library / "steamapps" / "common" / "Cyberpunk 2077"
            game.mkdir(parents=True)
            vdf = steam_root / "steamapps" / "libraryfolders.vdf"
            vdf.parent.mkdir(parents=True)
            escaped_library = str(secondary_library).replace("\\", "\\\\")
            vdf.write_text(
                f'"libraryfolders" {{ "1" {{ "path" "{escaped_library}" }} }}',
                encoding="utf-8",
            )

            found = nexus_links._find_cyberpunk_in_steam_roots([steam_root])

        self.assertEqual(found, game)

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
