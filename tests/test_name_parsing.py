import unittest

import nexus_links


class FilenameParsingTests(unittest.TestCase):
    def test_parses_standard_nexus_filename(self) -> None:
        parsed = nexus_links.extract_mod_meta(
            "Cyber Engine Tweaks-107-1-37-1-1730123456"
        )

        self.assertEqual(
            parsed,
            nexus_links.ParsedName(
                title="Cyber Engine Tweaks",
                mod_id=107,
                version="1.37.1",
                ts=1730123456,
            ),
        )

    def test_parses_vortex_filename_and_date(self) -> None:
        parsed = nexus_links.extract_mod_meta(
            "Sample Mod 12345 v1.2 2024-01-01T12-30-00Z abc123"
        )

        self.assertEqual(parsed.title, "Sample Mod")
        self.assertEqual(parsed.mod_id, 12345)
        self.assertEqual(parsed.version, "1.2")
        self.assertEqual(parsed.ts, 1704112200)

    def test_parses_manually_renamed_archive(self) -> None:
        parsed = nexus_links.extract_mod_meta("Example Mod-1234-1-0")

        self.assertEqual(parsed.title, "Example Mod")
        self.assertEqual(parsed.mod_id, 1234)
        self.assertEqual(parsed.version, "1.0")
        self.assertIsNone(parsed.ts)

    def test_rejects_file_id_but_removes_it_from_title(self) -> None:
        parsed = nexus_links.extract_mod_meta("Virtual Atelier-168466")

        self.assertEqual(parsed.title, "Virtual Atelier")
        self.assertIsNone(parsed.mod_id)
        self.assertEqual(parsed.rejected_id, 168466)

    def test_rejects_file_id_in_timestamped_nexus_filename(self) -> None:
        parsed = nexus_links.extract_mod_meta(
            "Virtual Atelier-168466-1-0-1730123456"
        )

        self.assertEqual(parsed.title, "Virtual Atelier")
        self.assertIsNone(parsed.mod_id)
        self.assertEqual(parsed.rejected_id, 168466)
        self.assertEqual(parsed.version, "1.0")
        self.assertEqual(parsed.ts, 1730123456)

    def test_extracts_browser_copy_suffix_when_name_has_no_id(self) -> None:
        parsed = nexus_links.extract_mod_meta("My Mod (2)")

        self.assertEqual(parsed.title, "My Mod")
        self.assertIsNone(parsed.mod_id)
        self.assertEqual(parsed.copy_index, 2)

    def test_cleans_nonstandard_filename_without_mod_id(self) -> None:
        parsed = nexus_links.extract_mod_meta("Киберпанк-мод-Пример")

        self.assertEqual(parsed.title, "Пример")
        self.assertIsNone(parsed.mod_id)


if __name__ == "__main__":
    unittest.main()
