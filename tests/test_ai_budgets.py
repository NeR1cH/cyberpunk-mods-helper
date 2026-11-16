"""S08: лимиты запросов OpenRouter настраиваются и честно описаны."""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nexus_links
from modaudit import limits as openrouter_limits


class BudgetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(openrouter_limits.configure_budgets)
        counter = patch.object(openrouter_limits, "_requests_this_process", 0)
        counter.start()
        self.addCleanup(counter.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.usage = self.root / "usage.json"

    def write_usage(self, used: int) -> None:
        self.usage.write_text(json.dumps({
            "date": datetime.now(timezone.utc).date().isoformat(), "requests": used,
        }), encoding="utf-8")


class BudgetConfigTests(BudgetTestCase):
    def test_default_per_run_budget_equals_the_daily_budget(self) -> None:
        self.assertEqual(openrouter_limits.current_budgets(), (10, 10))
        for ai_cfg in (None, {}, "broken"):
            with self.subTest(ai_cfg=ai_cfg):
                self.assertEqual(openrouter_limits.budgets_from_config(ai_cfg), (10, 10))

    def test_valid_budgets_are_read_from_the_ai_section(self) -> None:
        self.assertEqual(
            openrouter_limits.budgets_from_config({"daily_budget": 30, "per_run_budget": 6}),
            (30, 6),
        )
        self.assertEqual(openrouter_limits.budgets_from_config({"daily_budget": 0}), (0, 0))

    def test_invalid_budgets_fall_back_to_defaults_with_a_warning(self) -> None:
        for name in ("daily_budget", "per_run_budget"):
            for bad in (-1, True, "5", 2.5, None):
                with self.subTest(name=name, value=bad):
                    with self.assertLogs("modaudit", level="WARNING") as captured:
                        result = openrouter_limits.budgets_from_config({name: bad})
                    self.assertEqual(result, (10, 10))
                    self.assertTrue(any(f"ai.{name}" in line for line in captured.output))

    def test_configured_budgets_drive_reservation_and_reporting(self) -> None:
        openrouter_limits.configure_budgets(daily=4, per_run=3)

        self.assertEqual(openrouter_limits.remaining_daily_requests(self.usage), 4)
        granted = [openrouter_limits.reserve_request(self.usage) for _ in range(3)]
        with self.assertLogs("modaudit", level="WARNING"):
            refused = openrouter_limits.reserve_request(self.usage)
        self.assertEqual((granted, refused), ([True, True, True], False))
        self.assertEqual(openrouter_limits.remaining_daily_requests(self.usage), 1)

        openrouter_limits.configure_budgets()
        self.assertEqual(openrouter_limits.current_budgets(), (10, 10))

    def test_daily_budget_stops_requests_before_the_per_run_budget(self) -> None:
        openrouter_limits.configure_budgets(daily=1, per_run=5)

        self.assertTrue(openrouter_limits.reserve_request(self.usage))
        with self.assertLogs("modaudit", level="WARNING"):
            self.assertFalse(openrouter_limits.reserve_request(self.usage))

    def test_blocking_the_run_respects_a_raised_per_run_budget(self) -> None:
        openrouter_limits.configure_budgets(daily=10, per_run=6)

        self.assertTrue(openrouter_limits.reserve_request(self.usage))
        openrouter_limits.block_remaining_requests_this_run()
        with self.assertLogs("modaudit", level="WARNING"):
            self.assertFalse(openrouter_limits.reserve_request(self.usage))

    def test_negative_budgets_are_rejected_by_the_setter(self) -> None:
        with self.assertRaises(ValueError):
            openrouter_limits.configure_budgets(daily=-1)
        with self.assertRaises(ValueError):
            openrouter_limits.configure_budgets(per_run=-1)

    def test_progress_command_uses_the_configured_daily_budget(self) -> None:
        config = self.root / "config.json"
        config.write_text(json.dumps({"ai": {"daily_budget": 4}}), encoding="utf-8")
        self.write_usage(1)

        line = openrouter_limits.progress_line(
            config_path=config, usage_path=self.usage,
            analysis_cache_path=self.root / "missing-cache.json")
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        fallback = openrouter_limits.progress_line(
            config_path=broken, usage_path=self.usage,
            analysis_cache_path=self.root / "missing-cache.json")

        self.assertEqual(line, "3|0")
        self.assertEqual(fallback, "9|0")


class BudgetAnnouncementTests(BudgetTestCase):
    def settings(self, ai: dict) -> nexus_links.Settings:
        return nexus_links.Settings(None, None, None, self.root / "out.html",
                                    self.root / "quarantine", {"ai": ai})

    def announce(self, ai: dict, **arguments: object) -> str:
        args = Namespace(**{"ai_model": None, "ai_max_requests": None, **arguments})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            # Счётчик — во временной папке: настоящий openrouter_usage.json тесты не трогают.
            with patch("nexus_links.remaining_daily_requests",
                       side_effect=lambda: openrouter_limits.remaining_daily_requests(self.usage)):
                return nexus_links.apply_ai_budgets(self.settings(ai), args)

    def test_announcement_names_the_real_cap_and_where_it_comes_from(self) -> None:
        text = self.announce({"api_key": "key"})

        self.assertEqual(openrouter_limits.current_budgets(), (10, 10))
        self.assertIn("не более 10 запросов", text)
        self.assertIn("лимит за запуск 10 (по умолчанию равен суточному)", text)
        self.assertIn("на сегодня осталось 10 из 10 (по умолчанию)", text)

    def test_config_values_are_applied_and_attributed_to_config(self) -> None:
        self.write_usage(5)

        text = self.announce({"api_key": "key", "daily_budget": 7, "per_run_budget": 6})

        self.assertEqual(openrouter_limits.current_budgets(), (7, 6))
        self.assertIn("не более 2 запросов", text)
        self.assertIn("лимит за запуск 6 (config.json: ai.per_run_budget)", text)
        self.assertIn("на сегодня осталось 2 из 7 (config.json: ai.daily_budget)", text)

    def test_announcement_explains_shared_budget_and_failover(self) -> None:
        text = self.announce({"api_key": "key"})

        self.assertIn("общий", text)
        self.assertIn("запасную модель", text)

    def test_no_announcement_without_a_key_but_budgets_still_apply(self) -> None:
        text = self.announce({"per_run_budget": 4})

        self.assertEqual(text, "")
        self.assertEqual(openrouter_limits.current_budgets(), (10, 4))

    def test_nothing_is_read_or_announced_when_ai_is_not_used(self) -> None:
        args = Namespace(ai_model=None, ai_max_requests=None)
        with patch("nexus_links.remaining_daily_requests") as remaining:
            text = nexus_links.apply_ai_budgets(
                self.settings({"api_key": "key", "per_run_budget": 3}), args, announce=False)

        self.assertEqual(text, "")
        remaining.assert_not_called()
        self.assertEqual(openrouter_limits.current_budgets(), (10, 3))

    def test_unreadable_ledger_is_reported_instead_of_a_number(self) -> None:
        self.usage.write_text("{broken", encoding="utf-8")

        with self.assertLogs("modaudit", level="ERROR"):
            text = self.announce({"api_key": "key"})

        self.assertIn("счётчик", text)
        self.assertNotIn("не более", text)


class HonestHelpTests(unittest.TestCase):
    def test_help_says_that_the_per_run_budget_caps_ai_max_requests(self) -> None:
        parser = nexus_links.build_parser()
        scan = next(action for action in parser._actions
                    if getattr(action, "choices", None) and "scan" in action.choices
                    ).choices["scan"]
        text = " ".join(scan.format_help().split())

        self.assertIn("ai.per_run_budget", text)
        self.assertIn("ai.daily_budget", text)
        self.assertIn("ai.max_requests", text)

    def test_model_failover_spends_a_request(self) -> None:
        calls: list[str] = []

        def fake_http(url: str, key: str, payload: dict) -> dict:
            calls.append(payload["model"])
            if payload["model"] == "first":
                raise nexus_links.AIError("first: недоступна", status=503)
            return {"choices": [{"message": {"content": "[]"}}]}

        with patch("nexus_links.reserve_request", return_value=True) as reserve, \
                patch("nexus_links.wait_for_request_slot"), \
                patch("nexus_links._http_json", side_effect=fake_http):
            model, items = nexus_links._ai_ask(["first", "second"], "key", "text")

        self.assertEqual((model, items, calls), ("second", [], ["first", "second"]))
        self.assertEqual(reserve.call_count, 2)


if __name__ == "__main__":
    unittest.main()
