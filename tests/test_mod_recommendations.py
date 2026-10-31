from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import nexus_links
import mod_recommendations
import openrouter_limits


class OpenRouterLimitTests(unittest.TestCase):
    def test_ai_request_is_not_sent_when_budget_is_exhausted(self) -> None:
        with patch("nexus_links.reserve_request", return_value=False):
            with patch("nexus_links._http_json") as http_json:
                with self.assertRaises(nexus_links.AIError):
                    nexus_links._ai_ask(["test/model"], "key", "prompt")

        http_json.assert_not_called()

    def test_fatal_openrouter_error_blocks_remaining_requests_this_run(self) -> None:
        with patch("nexus_links.reserve_request", return_value=True):
            with patch("nexus_links.wait_for_request_slot"):
                with patch(
                    "nexus_links._http_json",
                    side_effect=nexus_links.AIError("unauthorized", status=401),
                ) as http_json:
                    with patch("nexus_links.block_remaining_requests_this_run") as block:
                        with self.assertRaises(nexus_links.AIError):
                            nexus_links._ai_ask(
                                ["test/model", "fallback/model"],
                                "key",
                                "prompt",
                            )

        http_json.assert_called_once()
        block.assert_called_once()

    def test_persistent_daily_and_per_run_budgets_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir) / "usage.json"
            with patch.object(openrouter_limits, "_requests_this_process", 0):
                self.assertTrue(openrouter_limits.reserve_request(usage_path, 5, 2))
                self.assertTrue(openrouter_limits.reserve_request(usage_path, 5, 2))
                self.assertFalse(openrouter_limits.reserve_request(usage_path, 5, 2))

            usage = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(usage["requests"], 2)

            with patch.object(openrouter_limits, "_requests_this_process", 0):
                self.assertTrue(openrouter_limits.reserve_request(usage_path, 5, 2))

            usage = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(usage["requests"], 3)

    def test_corrupt_usage_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir) / "usage.json"
            usage_path.write_text("{invalid", encoding="utf-8")
            with patch.object(openrouter_limits, "_requests_this_process", 0):
                self.assertFalse(openrouter_limits.reserve_request(usage_path))

            self.assertEqual(usage_path.read_text(encoding="utf-8"), "{invalid")

    def test_parallel_reservations_do_not_exceed_daily_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir) / "usage.json"
            with patch.object(openrouter_limits, "_requests_this_process", 0):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(executor.map(
                        lambda _: openrouter_limits.reserve_request(
                            usage_path,
                            daily_budget=5,
                            per_run_budget=20,
                        ),
                        range(12),
                    ))

            self.assertEqual(sum(results), 5)
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(usage["requests"], 5)

    def test_remaining_daily_budget_reads_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_path = Path(temp_dir) / "usage.json"
            self.assertEqual(openrouter_limits.remaining_daily_requests(usage_path), 10)
            self.assertTrue(openrouter_limits.reserve_request(
                usage_path,
                daily_budget=10,
                per_run_budget=10,
            ))
            self.assertEqual(openrouter_limits.remaining_daily_requests(usage_path), 9)

    def test_cached_analysis_page_count_reports_new_page_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "nexus_analysis_cache.json"
            self.assertEqual(openrouter_limits.cached_analysis_page_count(cache_path), 0)
            cache_path.write_text(
                json.dumps({"version": 1, "items": {"123": {}, "456": {}}}),
                encoding="utf-8",
            )
            self.assertEqual(openrouter_limits.cached_analysis_page_count(cache_path), 2)


class CleanSelectionTests(unittest.TestCase):
    def test_ai_max_requests_zero_disables_requests_and_negative_is_rejected(self) -> None:
        parser = nexus_links.build_parser()
        args = parser.parse_args(["scan", "--ai", "--ai-max-requests", "0"])
        rows = [Namespace(mod_id=None, title="Example mod", files=[])]
        settings = Namespace(cfg={"ai": {"api_key": "test-key"}})

        with patch("nexus_links.load_ai_cache", return_value={}):
            with patch("nexus_links._ai_ask") as ask:
                nexus_links.run_ai(rows, settings, args, online=True)

        ask.assert_not_called()
        with self.assertRaises(SystemExit):
            parser.parse_args(["scan", "--ai-max-requests", "-1"])

    def test_clean_rejects_non_object_selection_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selection_path = root / "selection.json"
            selection_path.write_text("[]", encoding="utf-8")
            settings = nexus_links.Settings(
                downloads=root / "downloads",
                vortex=None,
                game=None,
                out=root / "report.html",
                quarantine=root / "quarantine",
            )
            args = Namespace(
                selection=str(selection_path),
                no_verify=False,
                apply=False,
                mode="quarantine",
            )

            with patch("nexus_links.load_settings", return_value=settings):
                with self.assertLogs("modaudit", level="ERROR"):
                    self.assertEqual(nexus_links.cmd_clean(args), 2)

    def test_verify_entry_rejects_malformed_fields_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = {
                "path": str(root / "mod.zip"),
                "keeper": str(root / "keeper.zip"),
                "size": 1,
                "mtime": [],
                "reason": nexus_links.EXACT_DUP,
            }

            self.assertIsNotNone(
                nexus_links.verify_entry(entry, root, root / "quarantine", verify_hash=True)
            )

    def test_verify_entry_accepts_valid_exported_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = root / "candidate.zip"
            keeper = root / "keeper.zip"
            candidate.write_bytes(b"same contents")
            keeper.write_bytes(b"same contents")
            stat = candidate.stat()
            entry = {
                "path": str(candidate),
                "keeper": str(keeper),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "sha256": nexus_links.sha256_file(candidate),
                "reason": nexus_links.EXACT_DUP,
            }

            self.assertIsNone(
                nexus_links.verify_entry(entry, root, root / "quarantine", verify_hash=True)
            )


class InstallationNoteTests(unittest.TestCase):
    def test_manual_install_instructions_are_preserved(self) -> None:
        note = mod_recommendations.extract_installation_note(
            786,
            "[b]INSTALLATION:[/b]<br>Copy the INI into the Cyberpunk 2077 folder."
            "<br>Please endorse this mod.<br>[b]FEATURES:[/b]<br>Other description text.",
            checked_at=1,
        )

        self.assertEqual(note.method, "manual")
        self.assertIn("Copy the INI", note.excerpt)
        self.assertNotIn("Please endorse", note.excerpt)
        self.assertNotIn("Other description text", note.excerpt)

    def test_vortex_installation_requires_explicit_install_wording(self) -> None:
        explicit = mod_recommendations.extract_installation_note(
            1,
            "Installation using Vortex is supported.",
            checked_at=1,
        )
        mention_only = mod_recommendations.extract_installation_note(
            2,
            "Installation instructions follow. Vortex is a mod manager.",
            checked_at=1,
        )

        self.assertEqual(explicit.method, "vortex")
        self.assertEqual(mention_only.method, "unknown")

    def test_ambiguous_description_does_not_make_a_method_claim(self) -> None:
        note = mod_recommendations.extract_installation_note(
            1,
            "Install the mod as described on the Nexus page.",
            checked_at=1,
        )

        self.assertEqual(note.method, "unknown")

    def test_negative_installation_claims_are_not_classified_as_support(self) -> None:
        vortex = mod_recommendations.extract_installation_note(
            1,
            "Installation via Vortex is not supported.",
            checked_at=1,
        )
        manual = mod_recommendations.extract_installation_note(
            2,
            "INSTALLATION: Do not copy the files manually.",
            checked_at=1,
        )

        self.assertEqual(vortex.method, "unknown")
        self.assertEqual(manual.method, "unknown")

    def test_fetches_and_caches_public_nexus_installation_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "nexus_install_cache.json"
            response = MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps({
                "data": {
                    "description": "INSTALLATION:<br>Drag and drop the archive folder."
                }
            }).encode("utf-8")
            with patch("mod_recommendations.urllib.request.urlopen", return_value=response) as urlopen:
                with patch("mod_recommendations.time.sleep"):
                    notes = mod_recommendations.load_installation_notes({786}, cache_path, 1)

            self.assertEqual(notes[786].method, "manual")
            self.assertEqual(urlopen.call_count, 1)
            self.assertTrue(cache_path.is_file())
            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://api.nexusmods.com/v3/games/cyberpunk2077/mods/786",
            )
            self.assertIsNone(request.get_header("Authorization"))

            with patch("mod_recommendations.urllib.request.urlopen") as second_urlopen:
                cached_notes = mod_recommendations.load_installation_notes({786}, cache_path, 1)

        self.assertEqual(cached_notes[786].method, "manual")
        second_urlopen.assert_not_called()

    def test_permanent_http_error_is_cached_and_does_not_block_later_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "nexus_install_cache.json"
            denied = urllib.error.HTTPError(
                "https://api.nexusmods.com/v3/games/cyberpunk2077/mods/20405",
                403,
                "Forbidden",
                {},
                None,
            )
            response = MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps({
                "data": {"description": "INSTALLATION: Copy files to the game folder."}
            }).encode("utf-8")
            with patch(
                "mod_recommendations.urllib.request.urlopen",
                side_effect=[denied, response],
            ) as urlopen:
                with patch("mod_recommendations.time.sleep"):
                    notes = mod_recommendations.load_installation_notes(
                        {20405, 20406},
                        cache_path,
                        2,
                    )

            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(notes[20405].method, "unavailable")
            self.assertEqual(notes[20405].error_code, 403)
            self.assertEqual(notes[20406].method, "manual")
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["items"]["20405"]["error_code"], 403)

            with patch("mod_recommendations.urllib.request.urlopen") as second_urlopen:
                cached_notes = mod_recommendations.load_installation_notes(
                    {20405, 20406},
                    cache_path,
                    2,
                )

        self.assertEqual(cached_notes[20405].error_code, 403)
        second_urlopen.assert_not_called()

    def test_analysis_payload_uses_page_data_without_local_archive_names(self) -> None:
        record = mod_recommendations.NexusModRecord(
            mod_id=1,
            name="Night City Alive",
            summary="World overhaul",
            description="Author's complete public description",
            category="Gameplay",
            tags=("Overhaul",),
            fetched_at=1,
        )

        payload = mod_recommendations.build_analysis_payload([record])

        self.assertIn("Author's complete public description", payload)
        self.assertIn("Night City Alive", payload)
        self.assertNotIn("archive_path", payload)

    def test_public_page_is_cached_with_full_description_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "nexus_analysis_cache.json"
            response = MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps({
                "data": {
                    "name": "Example overhaul",
                    "summary": "Changes city behavior",
                    "description": "Complete author page text.",
                    "category": {"name": "Gameplay"},
                    "tags": [{"name": "Overhaul"}],
                }
            }).encode("utf-8")
            with patch("mod_recommendations.urllib.request.urlopen", return_value=response) as urlopen:
                records = mod_recommendations.fetch_nexus_mod_records({1234}, cache_path, 1)

            self.assertEqual(records[1234].description, "Complete author page text.")
            self.assertEqual(records[1234].category, "Gameplay")
            self.assertEqual(records[1234].tags, ("Overhaul",))
            request = urlopen.call_args.args[0]
            self.assertIsNone(request.get_header("Authorization"))

            with patch("mod_recommendations.urllib.request.urlopen") as second_urlopen:
                cached = mod_recommendations.fetch_nexus_mod_records({1234}, cache_path, 1)

        self.assertEqual(cached[1234].name, "Example overhaul")
        second_urlopen.assert_not_called()

    def test_recommendation_result_is_strictly_validated(self) -> None:
        record = mod_recommendations.NexusModRecord(
            mod_id=1234,
            name="Test mod",
            summary="",
            description="Test description",
            category="Gameplay",
            tags=(),
            fetched_at=1,
        )
        item = {
            "i": 0,
            "method": "vortex",
            "category": "major_overhaul",
            "impact": "high",
            "confidence": 82,
            "summary_ru": "Заметно меняет поведение города.",
            "rationale_ru": "Автор указывает поддержку менеджера.",
        }

        result = mod_recommendations.parse_recommendations([item], [record], "test/model")

        self.assertEqual(result[1234].method, "vortex")
        self.assertEqual(result[1234].confidence, 82)
        self.assertEqual(
            mod_recommendations.analysis_label(result[1234].category),
            "крупный переработчик",
        )
        invalid = dict(item, confidence=181)
        self.assertEqual(mod_recommendations.parse_recommendations([invalid], [record], "test/model"), {})

    def test_rendered_ai_recommendation_escapes_untrusted_model_text(self) -> None:
        record = mod_recommendations.NexusModRecord(
            mod_id=1234,
            name="Test mod",
            summary="",
            description="INSTALLATION: Copy files to the game folder.",
            category="Gameplay",
            tags=(),
            fetched_at=1,
            recommendation=mod_recommendations.ModRecommendation(
                method="unknown",
                category="gameplay",
                impact="medium",
                confidence=50,
                summary_ru="<script>alert(1)</script>",
                rationale_ru="Не удалось подтвердить метод.",
                model="test/model",
                analyzed_at=1,
            ),
        )

        rendered = nexus_links.render_mod_analysis(record)

        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("Фрагмент инструкции автора", rendered)
        self.assertIn("nexusmods.com/cyberpunk2077/mods/1234", rendered)

    def test_analysis_runner_uses_page_data_and_persists_ai_result(self) -> None:
        record = mod_recommendations.NexusModRecord(
            mod_id=1234,
            name="Night City Alive",
            summary="World overhaul",
            description="Author's page description",
            category="Gameplay",
            tags=("Overhaul",),
            fetched_at=1,
        )
        response = [{
            "i": 0,
            "method": "vortex",
            "category": "major_overhaul",
            "impact": "high",
            "confidence": 80,
            "summary_ru": "Перерабатывает городские системы.",
            "rationale_ru": "Автор указывает поддержку Vortex.",
        }]
        args = Namespace(
            nexus_max_requests=10,
            nexus_ai_max_mods=1,
            ai_model=None,
        )
        settings = Namespace(cfg={
            "ai": {
                "api_key": "test-key",
                "models": ["test/model"],
            }
        })
        rows = [Namespace(mod_id=1234)]
        with patch("nexus_links.fetch_nexus_mod_records", return_value={1234: record}):
            with patch("nexus_links._ai_ask", return_value=("test/model", response)) as ask:
                with patch("nexus_links.save_nexus_mod_records") as save:
                    result = nexus_links.run_nexus_ai_analysis(rows, settings, args)

        self.assertIn("Author's page description", ask.call_args.args[2])
        self.assertEqual(ask.call_args.kwargs["system_prompt"], mod_recommendations.ANALYSIS_SYSTEM_PROMPT)
        self.assertEqual(result[1234].recommendation.method, "vortex")
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
