import unittest
from unittest.mock import patch

import nexus_links


class AIBatchRunnerTests(unittest.TestCase):
    def test_runner_respects_request_cap_and_persists_each_batch(self) -> None:
        todo = ["first mod", "second mod", "third mod"]
        wanted = {key: (key.title(), [], []) for key in todo}
        responses = [
            ("fallback/model", [{"i": 0, "clean_title": "First Mod", "conf": "high"}]),
            ("fallback/model", [{"i": 0, "clean_title": "Second Mod", "conf": "high"}]),
        ]
        result: dict[str, nexus_links.AIInfo] = {}
        cache: dict[str, dict] = {}

        with patch.object(nexus_links, "AI_BATCH_SIZE", 1):
            with patch("nexus_links._ai_ask", side_effect=responses) as ask:
                with patch("nexus_links.save_ai_cache") as save:
                    nexus_links._run_ai_batches(
                        todo,
                        wanted,
                        ["primary/model", "fallback/model"],
                        "mock-key",
                        2,
                        result,
                        cache,
                    )

        self.assertEqual(ask.call_count, 2)
        self.assertEqual(ask.call_args_list[0].args[0], ["primary/model", "fallback/model"])
        self.assertEqual(ask.call_args_list[1].args[0], ["fallback/model", "primary/model"])
        self.assertEqual(set(result), {"first mod", "second mod"})
        self.assertEqual(set(cache), {"first mod", "second mod"})
        self.assertEqual(save.call_count, 2)


if __name__ == "__main__":
    unittest.main()
