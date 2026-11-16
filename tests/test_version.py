"""S10: одна версия на весь проект, CHANGELOG, лицензия и CI."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import nexus_links

ROOT = Path(nexus_links.__file__).resolve().parent


class VersionTests(unittest.TestCase):
    def test_version_is_defined_once_and_is_pep440(self) -> None:
        version = nexus_links.__version__

        self.assertRegex(version, r"^\d+\.\d+\.\d+(\.dev\d+|(a|b|rc)\d+)?$")
        literal = re.compile(r"\b\d+\.\d+\.\d+(?:\.dev\d+)?\b")
        owners = [path.name for path in sorted([*ROOT.glob("*.py"), *(ROOT / "modaudit").glob("*.py")])
                  if version in literal.findall(path.read_text(encoding="utf-8"))]
        self.assertEqual(owners, ["nexus_links.py"])

    def test_version_flag_prints_the_version_and_exits_cleanly(self) -> None:
        for argv in (["--version"], ["scan", "--help"]):
            with self.subTest(argv=argv):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    nexus_links.main(argv)
                self.assertEqual(raised.exception.code, 0)
        self.assertIn(nexus_links.__version__, self.version_output())

    def version_output(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            nexus_links.main(["--version"])
        return output.getvalue()

    def test_dashboard_footer_shows_the_version(self) -> None:
        with patch("nexus_links.load_custom_blacklist", return_value={}):
            with patch.object(nexus_links, "__version__", "9.8.7"):
                page = nexus_links.render_dashboard({}, [], [])

        footer = re.search(r'<footer class="page-footer">(.*?)</footer>', page, re.DOTALL).group(1)
        self.assertIn("v9.8.7", footer)
        self.assertNotIn("@@", footer)

    def test_changelog_covers_the_current_and_the_last_released_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        base = nexus_links.__version__.split(".dev")[0]

        self.assertIn(base, changelog)
        self.assertIn("## 1.2.0", changelog)

    def test_license_is_mit(self) -> None:
        self.assertTrue((ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"))

    def test_ci_runs_the_documented_checks_on_both_systems(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

        for needle in ("ubuntu-latest", "windows-latest", '"3.10"', '"3.13"',
                       "python -m unittest discover -s tests -q", "python -m compileall -q .",
                       "git diff --check", "ruff check --no-fix"):
            with self.subTest(needle=needle):
                self.assertIn(needle, workflow)
        for forbidden in ("--fix ", "ruff format", "mypy", "pyinstaller"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, workflow.lower().replace("--no-fix", ""))


if __name__ == "__main__":
    unittest.main()
