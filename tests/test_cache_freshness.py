import unittest

import mod_recommendations


class CacheFreshnessTests(unittest.TestCase):
    def test_non_finite_cache_timestamps_are_stale(self) -> None:
        now = 1_000_000.0
        cached_at = {
            1: float("nan"),
            2: float("inf"),
            3: now + 60,
        }

        stale = mod_recommendations._stale_mod_ids({1, 2, 3}, cached_at, now)

        self.assertEqual(stale, [1, 2])


if __name__ == "__main__":
    unittest.main()
