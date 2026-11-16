"""Поведенческие тесты JS-логики дашборда. Нужен Node.js; без него тесты пропускаются."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import nexus_links

NODE = shutil.which("node")
SERVER_SOURCE = Path(nexus_links.__file__).with_name("server.py")


def pure_logic() -> str:
    """Блок чистых функций из шаблона: между метками <pure-logic> и </pure-logic>."""
    template = nexus_links.TEMPLATE_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"// <pure-logic>(.*?)// </pure-logic>", template, re.DOTALL)
    assert len(blocks) == 1, "в шаблоне должен быть ровно один блок <pure-logic>"
    return blocks[0]


@unittest.skipUnless(NODE, "Node.js не установлен: тесты JS-логики дашборда пропущены")
class DashboardLogicTests(unittest.TestCase):
    def run_js(self, body: str) -> str:
        script = (
            "'use strict';\n"
            "const assert = require('node:assert/strict');\n"
            + pure_logic() + "\n" + body
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "check.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [NODE, str(path)], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    FILES = """
        const files = [
            { fid: 0, s: 'downloads', vd: 'exact_dup', p: 'D:\\\\dl\\\\copy.zip', z: 10, m: 1.5, h: 'aa', k: 'D:\\\\vx\\\\copy.zip' },
            { fid: 1, s: 'downloads', vd: 'older', p: 'D:\\\\dl\\\\old.zip', z: 20, m: 2, h: '', k: 'D:\\\\vx\\\\new.zip' },
            { fid: 2, s: 'downloads', vd: 'keep', p: 'D:\\\\dl\\\\keep.zip', z: 30, m: 3, h: '', k: '' },
            { fid: 3, s: 'vortex', vd: 'keep', p: 'D:\\\\vx\\\\copy.zip', z: 10, m: 4, h: 'aa', k: '' },
        ];
        const ids = (list) => list.map(f => f.fid);
    """

    def test_only_ticked_candidates_are_selected(self) -> None:
        self.run_js(self.FILES + """
            assert.deepEqual(ids(pickSelected(files, new Set([0]))), [0]);
            assert.deepEqual(ids(pickSelected(files, new Set([1]))), [1]);
            assert.deepEqual(ids(pickSelected(files, new Set())), []);
            // Файл, который не кандидат (оставляемый или из Vortex), не выбирается никогда.
            assert.deepEqual(ids(pickSelected(files, new Set([0, 1, 2, 3]))), [0, 1]);
            // После того как файл убран со страницы, выбор не съезжает на соседний.
            const rest = files.filter(f => f.fid !== 0);
            assert.deepEqual(ids(pickSelected(rest, new Set([1]))), [1]);
            assert.deepEqual(ids(pickSelected(rest, new Set([0]))), []);
        """)

    def test_unticked_file_is_absent_from_selection_json(self) -> None:
        output = self.run_js(self.FILES + """
            const selection = buildSelection(pickSelected(files, new Set([1])), '2026-11-16T00:00:00.000Z');
            process.stdout.write(JSON.stringify(selection));
        """)

        self.assertEqual(json.loads(output), {
            "created": "2026-11-16T00:00:00.000Z",
            "files": [{
                "path": "D:\\dl\\old.zip", "size": 20, "mtime": 2, "sha256": "",
                "reason": "older", "keeper": "D:\\vx\\new.zip",
            }],
        })

    def test_exported_selection_passes_the_python_clean_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            downloads = Path(temp_dir) / "Загрузки модов"
            downloads.mkdir()
            (downloads / "Sample Mod-1234-1-0.zip").write_bytes(b"identical archive")
            (downloads / "Sample Mod-1234-1-0 (1).zip").write_bytes(b"identical archive")
            archives, _ = nexus_links._scan_archive_roots(downloads, None)
            nexus_links._prepare_scan_archives(archives)
            rows = nexus_links.build_rows(archives, {})
            _, file_json = nexus_links._dashboard_file_data(rows, archives)

            output = self.run_js(f"""
                const files = {file_json};
                files.forEach((f, i) => {{ f.fid = i; }});
                const ticked = new Set(files.filter(f => f.vd === 'exact_dup').map(f => f.fid));
                process.stdout.write(JSON.stringify(buildSelection(pickSelected(files, ticked), 'now')));
            """)
            entries = json.loads(output)["files"]
            checks = list(nexus_links.iter_clean_checks(
                entries, downloads, downloads / nexus_links.QUARANTINE_DIRNAME))

        self.assertEqual([Path(entry["path"]).name for entry in entries],
                         ["Sample Mod-1234-1-0 (1).zip"])
        self.assertEqual([problem for _, problem in checks], [None])

    def test_size_and_name_formatting(self) -> None:
        self.run_js("""
            assert.equal(fmtSize(0), '0 КБ');
            assert.equal(fmtSize(1536), '2 КБ');
            assert.equal(fmtSize(5 * 1048576), '5.0 МБ');
            assert.equal(fmtSize(3 * 1073741824), '3.0 ГБ');
            assert.equal(baseName('D:\\\\Загрузки модов\\\\Мод (1).zip'), 'Мод (1).zip');
            assert.equal(baseName('/home/user/dl/mod.zip'), 'mod.zip');
        """)

    def test_every_clean_status_of_the_server_has_a_message(self) -> None:
        source = SERVER_SOURCE.read_text(encoding="utf-8")
        handler = source[source.index("def handle_clean("):source.index("return DashboardHandler")]
        statuses = sorted(set(re.findall(r'reply_json\(\d+, "([a-z_]+)"\)', handler)))
        self.assertIn("preview_required", statuses)

        self.run_js(f"""
            const fallback = cleanMessage('no_such_status');
            for (const status of {json.dumps(statuses + ["not_found", "gone", "forbidden_origin"])}) {{
                assert.notEqual(cleanMessage(status), fallback, status);
            }}
        """)


if __name__ == "__main__":
    unittest.main()
