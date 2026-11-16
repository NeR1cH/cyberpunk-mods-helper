"""R01, шаг 2: разнести nexus_links.py по модулям пакета modaudit.

Запуск: python r01_split.py report   — показать ссылки «вверх» по слоям и неразмеченные имена
        python r01_split.py write    — записать модули и фасад
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "modaudit"
MODE = sys.argv[1]
SRC = (ROOT / "nexus_links.py").read_text(encoding="utf-8")
LINES = SRC.split("\n")
TREE = ast.parse(SRC)

LAYERS = ["config", "names", "scan", "analysis", "ai", "render", "clean", "cli"]
DOCS = {
    "config": "Версия, пути, константы и загрузка настроек.",
    "names": "Разбор имён архивов: название, Mod ID, версия, сравнение свежести.",
    "scan": "Обход папок, хеши архивов, инвентарь папки игры и поиск игры в Steam.",
    "analysis": "Точные копии, старые версии, группировка в строки отчёта, чёрный список.",
    "ai": "Клиент OpenRouter: распознавание названий и анализ страниц Nexus, кэш ответов.",
    "render": "HTML-дашборд.",
    "clean": "Проверка списка очистки и перенос файлов в карантин или Корзину.",
    "cli": "Команды scan, clean, ai-models и разбор аргументов.",
}
SECTION_MODULE = {
    "(начало)": "config", "ПАРСИНГ ИМЁН": "names", "ДАННЫЕ": "scan", "АНАЛИЗ": "analysis",
    "ИИ-ПОМОЩНИК (OpenRouter)": "ai", "HTML": "render", "НАСТРОЙКИ": "config",
    "КОМАНДА scan": "cli", "КОМАНДА clean": "clean", "CLI": "cli",
}
OVERRIDE = {
    "CORE_FRAMEWORKS": "scan", "KEEP": "scan", "EXACT_DUP": "scan", "OLDER": "scan", "NEWER": "scan",
    "STALE": "scan",
    "OUTDATED_OR_DANGEROUS": "analysis", "KNOWN_MOD_IDS": "analysis", "ModRow": "analysis",
    "STATUS_ORDER": "analysis",
    "DEFAULT_MAX_MOD_ID": "config", "MAX_MOD_ID": "config", "configured_max_mod_id": "config",
    "set_max_mod_id": "config",
    "cmd_ai_models": "cli", "_write_models_to_config": "cli", "cmd_clean": "cli",
    "known_ids_from_ai": "ai", "clean_english_title": "ai",
}
OVERRIDE.update(dict(arg.split("=") for arg in sys.argv[2:]))

HEADER = re.compile(r"# =+ (.+?) =+$")
heads = {i + 1: m.group(1).strip() for i, line in enumerate(LINES) if (m := HEADER.match(line))}


def section(lineno: int) -> str:
    name = "(начало)"
    for mark in sorted(heads):
        if mark <= lineno:
            name = heads[mark]
    return name


def defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


segments: dict[str, list[str]] = {m: [] for m in LAYERS}
where: dict[str, str] = {}
import_lines: list[str] = []
docstring = ""
prev_end = 0
for index, node in enumerate(TREE.body):
    start, end = prev_end + 1, node.end_lineno
    text = [line for line in LINES[start - 1:end] if not HEADER.match(line)]
    prev_end = end
    if index == 0 and isinstance(node, ast.Expr):
        docstring = "\n".join(text)
        continue
    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
        continue
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        import_lines.append("\n".join(LINES[node.lineno - 1:end]))
        continue
    if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
        continue
    names = defined_names(node)
    assert names, ast.unparse(node)[:80]
    if names == ["log"]:
        continue
    module = OVERRIDE.get(names[0]) or SECTION_MODULE[section(node.lineno)]
    for name in names:
        where[name] = module
    segments[module].append("\n".join(text).strip("\n"))

# имена из вспомогательных модулей, которые раньше импортировались в nexus_links
helper_names = set()
for line in import_lines:
    for stmt in ast.parse(line).body:
        if isinstance(stmt, ast.ImportFrom) and (stmt.module or "").startswith("modaudit"):
            helper_names.update(alias.asname or alias.name for alias in stmt.names)

bodies: dict[str, str] = {}
problems: list[str] = []
for module in LAYERS:
    body = "\n\n\n".join(segments[module])
    tree = ast.parse(body)
    body_lines = body.split("\n")
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    # Аннотации в кавычках ("Settings") — тоже использование имени.
    used |= {n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in where}
    needed: dict[str, set[str]] = {}
    for name in sorted(used):
        owner = where.get(name)
        if (module, name) == ("config", "find_cyberpunk_install"):
            continue   # единственная ссылка вверх: импортируется внутри load_settings
        if owner and owner != module and name != "MAX_MOD_ID":
            needed.setdefault(owner, set()).add(name)
            if LAYERS.index(owner) > LAYERS.index(module):
                problems.append(f"вверх: {module} → {owner}.{name}")
    # MAX_MOD_ID меняется во время работы, поэтому читается через модуль config, а не копией.
    if module != "config":
        spots = sorted({(n.lineno, n.col_offset) for n in ast.walk(tree)
                        if isinstance(n, ast.Name) and n.id == "MAX_MOD_ID"}, reverse=True)
        for lineno, col in spots:
            raw = body_lines[lineno - 1].encode("utf-8")
            assert raw[col:col + 10] == b"MAX_MOD_ID", (module, lineno)
            body_lines[lineno - 1] = (raw[:col] + b"config.MAX_MOD_ID" + raw[col + 10:]).decode("utf-8")
        if spots:
            needed.setdefault("", set()).add("config")
    body = "\n".join(body_lines)
    cross = []
    if "config" in needed.pop("", set()):
        cross.append("from modaudit import config")
    for owner in LAYERS:
        if owner in needed:
            cross.append(f"from modaudit.{owner} import (\n" +
                         "".join(f"    {n},\n" for n in sorted(needed[owner])) + ")")
    head = [f'"""{DOCS[module]}"""', "", "from __future__ import annotations", ""]
    head += import_lines + ["from modaudit import PROJECT_DIR"] + cross
    head += ["", 'log = logging.getLogger("modaudit")', "", ""]
    bodies[module] = "\n".join(head) + body + "\n"

if MODE == "report":
    print("\n".join(sorted(set(problems))) or "ссылок вверх нет")
    print({m: len(segments[m]) for m in LAYERS})
    sys.exit(0)

assert not problems, problems
for module, text in bodies.items():
    if module == "config":
        text = text.replace("SCRIPT_DIR = Path(__file__).resolve().parent\n", "SCRIPT_DIR = PROJECT_DIR\n")
        lazy = '    game = _as_path(getattr(args, "game", None) or cfg.get("game")) or find_cyberpunk_install()\n'
        assert text.count(lazy) == 1
        text = text.replace(lazy, "    # Импорт внутри функции: scan сам берёт константы из config, на уровне модуля вышел бы цикл.\n"
                                  "    from modaudit.scan import find_cyberpunk_install\n\n" + lazy)
        assert "SCRIPT_DIR = PROJECT_DIR" in text
    (PKG / f"{module}.py").write_text(text, encoding="utf-8")

facade = docstring + "\nimport sys\n\n" + "".join(
    f"from modaudit.{m} import *  # noqa: F401,F403\n" for m in LAYERS
) + '''from modaudit.cli import main
from modaudit.config import __version__  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
'''
(ROOT / "nexus_links.py").write_text(facade, encoding="utf-8")
(Path(__file__).with_name("r01_where.json")).write_text(
    __import__("json").dumps({"where": where, "helpers": sorted(helper_names)}, ensure_ascii=False, indent=1),
    encoding="utf-8")
print("записано:", {m: len(t.split(chr(10))) for m, t in bodies.items()})
