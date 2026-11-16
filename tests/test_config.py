"""S09: один источник констант и настроек для ядра, сервера и страницы."""
from __future__ import annotations

from argparse import Namespace
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import nexus_links
import server as dashboard_server

PROJECT_ROOT = Path(nexus_links.__file__).resolve().parent


def settings_with(cfg: object) -> nexus_links.Settings:
    return nexus_links.Settings(None, None, None, Path("out.html"), Path("quarantine"), cfg)


class SingleSourceTests(unittest.TestCase):
    def test_server_has_no_copies_of_core_constants(self) -> None:
        source = (PROJECT_ROOT / "server.py").read_text(encoding="utf-8")

        for needle in ("35000", "786", "3560", "4088", "BUILTIN_MOD_IDS"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_dashboard_template_has_no_copy_of_the_builtin_blacklist(self) -> None:
        template = nexus_links.TEMPLATE_PATH.read_text(encoding="utf-8")

        for mod_id in nexus_links.OUTDATED_OR_DANGEROUS:
            with self.subTest(mod_id=mod_id):
                self.assertIsNone(re.search(rf"\b{mod_id}\b", template))

    def test_page_data_carries_the_builtin_blacklist_ids(self) -> None:
        with patch("nexus_links.load_custom_blacklist", return_value={}):
            with patch.dict(nexus_links.OUTDATED_OR_DANGEROUS, {4242: "тест"}, clear=True):
                page = nexus_links.render_dashboard({}, [], [])
        meta = json.loads(re.search(
            r'<script id="modAuditMeta" type="application/json">(.*?)</script>',
            page, re.DOTALL).group(1))

        self.assertEqual(meta["builtin_blacklist"], [4242])

    def test_status_order_is_defined_once(self) -> None:
        source = (PROJECT_ROOT / "nexus_links.py").read_text(encoding="utf-8")

        self.assertEqual(len(re.findall(r'\{"danger": 0, "dup": 1', source)), 1)

    def test_max_mod_id_has_one_default_and_one_validation(self) -> None:
        default = nexus_links.DEFAULT_MAX_MOD_ID
        source = (PROJECT_ROOT / "nexus_links.py").read_text(encoding="utf-8")

        self.assertEqual(source.count("35000"), 1)
        self.assertEqual(nexus_links.configured_max_mod_id({"max_mod_id": 50000}), 50000)
        for bad in (None, {}, {"max_mod_id": 0}, {"max_mod_id": -5}, {"max_mod_id": True},
                    {"max_mod_id": "50000"}, {"max_mod_id": 1.5}):
            with self.subTest(cfg=bad):
                self.assertEqual(nexus_links.configured_max_mod_id(bad), default)


class LoadSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(nexus_links.set_max_mod_id)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = Path(self.temp_dir.name) / "config.json"
        steam = patch("nexus_links.find_cyberpunk_install", return_value=None)
        steam.start()
        self.addCleanup(steam.stop)

    def load(self) -> nexus_links.Settings:
        return nexus_links.load_settings(Namespace(config=str(self.config)))

    def test_broken_config_raises_config_error_instead_of_exiting(self) -> None:
        for content in ("{not json", "[]", '"text"'):
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                with self.assertRaises(nexus_links.ConfigError) as raised:
                    self.load()
                self.assertIn(str(self.config), str(raised.exception))

    def test_commands_turn_config_error_into_exit_code_2(self) -> None:
        self.config.write_text("{not json", encoding="utf-8")
        scan_args = nexus_links.build_parser().parse_args(
            ["scan", "--config", str(self.config), "--no-open"])
        clean_args = nexus_links.build_parser().parse_args(
            ["clean", "selection.json", "--config", str(self.config)])

        for args in (scan_args, clean_args):
            with self.subTest(command=args.func.__name__):
                with self.assertLogs("modaudit", level="ERROR") as captured:
                    self.assertEqual(args.func(args), 2)
                self.assertTrue(any(str(self.config) in line for line in captured.output))

    def test_max_mod_id_is_applied_through_the_setter_and_can_be_reset(self) -> None:
        self.config.write_text(json.dumps({"max_mod_id": 50000}), encoding="utf-8")

        self.load()
        self.assertEqual(nexus_links.MAX_MOD_ID, 50000)
        self.assertTrue(nexus_links.is_valid_mod_id(45000))

        nexus_links.set_max_mod_id()
        self.assertEqual(nexus_links.MAX_MOD_ID, nexus_links.DEFAULT_MAX_MOD_ID)
        self.assertFalse(nexus_links.is_valid_mod_id(45000))

    def test_config_without_max_mod_id_restores_the_default(self) -> None:
        nexus_links.set_max_mod_id(50000)
        self.config.write_text("{}", encoding="utf-8")

        self.load()

        self.assertEqual(nexus_links.MAX_MOD_ID, nexus_links.DEFAULT_MAX_MOD_ID)


class ResolveOpenRouterTests(unittest.TestCase):
    def resolve(self, cfg: object, ai_model: str | None = None,
                env: dict[str, str] | None = None) -> tuple[str, list[str]]:
        with patch.dict(os.environ, env or {}, clear=False):
            if env is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            return nexus_links.resolve_openrouter(settings_with(cfg), Namespace(ai_model=ai_model))

    def test_environment_key_wins_over_config(self) -> None:
        cfg = {"ai": {"api_key": "from-config"}}

        self.assertEqual(self.resolve(cfg)[0], "from-config")
        self.assertEqual(self.resolve(cfg, env={"OPENROUTER_API_KEY": " from-env "})[0], "from-env")

    def test_missing_or_placeholder_key_is_empty(self) -> None:
        for cfg in (None, {}, {"ai": "broken"}, {"ai": {}}, {"ai": {"api_key": "YOUR_KEY_HERE"}},
                    {"ai": {"api_key": "your-openrouter-key"}}, {"ai": {"api_key": "  "}}):
            with self.subTest(cfg=cfg):
                self.assertEqual(self.resolve(cfg)[0], "")

    def test_models_come_from_cli_then_config_then_defaults(self) -> None:
        cfg = {"ai": {"models": ["first/model", " ", "second/model"]}}

        self.assertEqual(self.resolve(cfg)[1], ["first/model", "second/model"])
        self.assertEqual(self.resolve(cfg, ai_model="cli/model")[1], ["cli/model"])
        for broken in ({}, {"ai": {"models": []}}, {"ai": {"models": "one/model"}}):
            with self.subTest(cfg=broken):
                self.assertEqual(self.resolve(broken)[1], nexus_links.DEFAULT_AI_MODELS)

    def test_both_ai_features_use_the_shared_resolver(self) -> None:
        source = (PROJECT_ROOT / "nexus_links.py").read_text(encoding="utf-8")

        self.assertEqual(source.count('os.environ.get("OPENROUTER_API_KEY"'), 1)
        self.assertEqual(source.count("resolve_openrouter(settings, args)"), 2)


class ServerUsesCoreConstantsTests(unittest.TestCase):
    def test_server_asks_the_core_about_builtin_ids_and_the_id_limit(self) -> None:
        source = (PROJECT_ROOT / "server.py").read_text(encoding="utf-8")

        self.assertIn("nexus_links.OUTDATED_OR_DANGEROUS", source)
        self.assertIn("nexus_links.configured_max_mod_id(", source)
        self.assertFalse(hasattr(dashboard_server, "DEFAULT_MAX_MOD_ID"))


if __name__ == "__main__":
    unittest.main()
