import tempfile
import unittest
from pathlib import Path

import nexus_links


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
    def test_same_mod_id_in_both_sources_is_one_group(self) -> None:
        files = [
            archive(0, "downloads", "Sample Mod", 1234),
            archive(1, "vortex", "Sample Mod", 1234),
        ]

        rows = nexus_links.build_rows(files, {})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cats, {"all"})
        self.assertEqual(len(rows[0].files), 2)
        self.assertIn("ID в имени архива", rows[0].grouping_reason)

    def test_title_only_group_can_span_both_sources(self) -> None:
        files = [
            archive(0, "downloads", "Unknown Mod"),
            archive(1, "vortex", "Unknown Mod"),
        ]

        rows = nexus_links.build_rows(files, {})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cats, {"all"})
        self.assertEqual(rows[0].grouping_reason, "очищенное название архива (ID не распознан)")

    def test_source_filters_only_include_exclusive_groups(self) -> None:
        files = [
            archive(0, "downloads", "Downloads Only", 1),
            archive(1, "vortex", "Vortex Only", 2),
            archive(2, "downloads", "Shared", 3),
            archive(3, "vortex", "Shared", 3),
        ]

        rows = {row.title: row for row in nexus_links.build_rows(files, {})}

        self.assertIn("local", rows["Downloads Only"].cats)
        self.assertNotIn("vortex", rows["Downloads Only"].cats)
        self.assertIn("vortex", rows["Vortex Only"].cats)
        self.assertNotIn("local", rows["Vortex Only"].cats)
        self.assertNotIn("local", rows["Shared"].cats)
        self.assertNotIn("vortex", rows["Shared"].cats)
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


if __name__ == "__main__":
    unittest.main()
