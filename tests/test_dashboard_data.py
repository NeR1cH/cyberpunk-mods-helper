import json
from pathlib import Path
import unittest

import nexus_links


class DashboardDataTests(unittest.TestCase):
    def test_file_payload_preserves_display_order_keepers_and_script_escaping(self) -> None:
        candidate = nexus_links.Archive(
            id=1,
            source="downloads",
            path=Path("unsafe.zip"),
            size=10,
            mtime=1,
            meta=nexus_links.ParsedName("</script>", 1234),
            verdict=nexus_links.EXACT_DUP,
            keeper=2,
        )
        keeper = nexus_links.Archive(
            id=2,
            source="vortex",
            path=Path("keeper.zip"),
            size=10,
            mtime=1,
            meta=nexus_links.ParsedName("Unsafe Mod", 1234),
        )
        row = nexus_links.ModRow(
            mod_id=1234,
            title="Unsafe Mod",
            files=[keeper, candidate],
            danger="",
            status="dup",
            actions=["Keep Vortex copy"],
            cats={"all", "dup"},
            grouping_reason="same mod ID",
        )

        index_of, payload = nexus_links._dashboard_file_data([row], [candidate, keeper])
        records = json.loads(payload)

        self.assertEqual(index_of, {2: 0, 1: 1})
        self.assertEqual([record["n"] for record in records], ["keeper.zip", "unsafe.zip"])
        self.assertEqual(records[1]["t"], "</script>")
        self.assertEqual(records[1]["k"], str(keeper.path))
        self.assertNotIn("</script>", payload.lower())


if __name__ == "__main__":
    unittest.main()
