# -*- coding: utf-8 -*-
"""
Cyberpunk 2077 Mod Auditor.

Сканирует архивы модов в Downloads и в папке загрузок Vortex, находит точные
копии (размер + SHA-256), старые версии и «хлам», строит HTML-дашборд.
Сам ничего не удаляет: дашборд выгружает список выбранных файлов, а команда
`clean` переносит их в карантин (или в Корзину) после повторной проверки.

    python nexus_links.py                       # собрать дашборд
    python nexus_links.py --downloads D:\\dl --vortex E:\\vx --game "D:\\Steam\\...\\Cyberpunk 2077"
    python nexus_links.py clean selection.json  # dry-run
    python nexus_links.py clean selection.json --apply

    python nexus_links.py --ai                  # + ИИ (OpenRouter): английские названия и поиск Nexus
    python nexus_links.py --ai --ai-all         # то же, но для всех модов, а не только без ID
    python nexus_links.py ai-models             # какие бесплатные модели есть сейчас
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import sys
import time
import webbrowser
import zipfile
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
import urllib.error
import urllib.request
from urllib.parse import quote, quote_plus

from mod_categories import build_scan_plan, source_categories, summarize_sources
from openrouter_limits import (
    block_remaining_requests_this_run,
    reserve_request,
    wait_for_request_slot,
)
from mod_recommendations import (
    InstallationNote,
    NexusModRecord,
    ANALYSIS_BATCH_SIZE,
    ANALYSIS_SYSTEM_PROMPT,
    analysis_label,
    build_analysis_payload,
    extract_installation_note,
    fetch_nexus_mod_records,
    format_checked_date,
    load_installation_notes,
    method_label,
    parse_recommendations,
    save_nexus_mod_records,
)

log = logging.getLogger("modaudit")

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SCRIPT_DIR / "dashboard_template.html"
CONFIG_PATH = SCRIPT_DIR / "config.json"
BLACKLIST_PATH = SCRIPT_DIR / "blacklist.json"

GAME_NAME = "cyberpunk2077"
EXTENSIONS = {".zip", ".rar", ".7z"}
QUARANTINE_DIRNAME = "_mod_audit_quarantine"

# Проверка по конкретным файлам: папка (r6\scripts, red4ext) остаётся и после
# удаления мода, поэтому на неё положиться нельзя. Достаточно одного найденного файла.
CORE_FRAMEWORKS: dict[str, list[str]] = {
    "Cyber Engine Tweaks (CET)": [r"bin\x64\plugins\cyber_engine_tweaks.asi"],
    "RED4ext": [r"red4ext\RED4ext.dll", r"bin\x64\winmm.dll"],
    "Redscript": [r"engine\tools\scc.exe"],
    "ArchiveXL": [r"red4ext\plugins\ArchiveXL\ArchiveXL.dll"],
    "TweakXL": [r"red4ext\plugins\TweakXL\TweakXL.dll"],
    "Codeware": [r"red4ext\plugins\Codeware\Codeware.dll"],
    "Equipment-EX": [
        r"red4ext\plugins\EquipmentEx\EquipmentEx.dll",
        r"red4ext\plugins\Equipment-EX\Equipment-EX.dll",
        r"red4ext\plugins\EquipmentEx",
        r"red4ext\plugins\Equipment-EX",
        r"r6\scripts\EquipmentEx",
        r"r6\scripts\Equipment-EX",
    ],
    "Mod Settings": [r"red4ext\plugins\mod_settings\mod_settings.dll"],
    "Native Settings UI": [r"bin\x64\plugins\cyber_engine_tweaks\mods\nativeSettings\init.lua"],
    "REDmod": [r"tools\redmod\bin\redMod.exe"],
}

# Базовый чёрный список. Можно дополнить/переопределить файлом blacklist.json:
# {"786": {"reason": "...", "game_version": "2.1", "date": "2024-05-01"}}
OUTDATED_OR_DANGEROUS: dict[int, str] = {
    786: "General AIO Optimization (патч 1.1 — ломает память в 2.x)",
    3560: "Metro System (в игре официальное метро NCART, мод сломает игру)",
    4088: "Russian translation for Metro System (не нужен без мода)",
}

# Встроенный словарь известных core-модов: имя → Mod ID на Nexus. Сопоставление точное, но
# нечувствительное к регистру, пробелам, дефисам, версиям, «Cyberpunk 2077» и суффиксам вроде
# «Mod»/«Patch»: «Cyber Engine Tweaks», «CyberEngineTweaks» и «cyber-engine-tweaks 1.37» — один мод,
# а «Virtual Atelier Addon» — уже другой (поэтому вхождение по подстроке не используется).
# Совпадение даёт прямую ссылку #ID без поиска. Имя берётся из имени файла и из ответа ИИ.
KNOWN_MOD_IDS: dict[str, int] = {
    "tweakxl": 4197,
    "redscript": 1511,
    "input loader": 4574,
    "mod settings": 4885,
    "auto loot": 5202,
    "virtual atelier": 2987,
    "virtual car dealer": 4454,
    "judy romance enhanced": 4508,
    "equipment-ex": 3783,
    "archivexl": 4198,
    "codeware": 7780,
    "cyber engine tweaks": 107,
    "native settings ui": 3518,
}

# Вердикты для отдельного архива
KEEP = "keep"
EXACT_DUP = "exact_dup"   # байт-в-байт копия другого файла (SHA-256)
OLDER = "older"           # того же файла мода есть версия новее
NEWER = "newer"           # в Downloads новее, чем в Vortex (информация)
STALE = "stale"           # в Vortex старее, чем в Downloads (информация)


# ==================== ПАРСИНГ ИМЁН ====================

@dataclass(frozen=True)
class ParsedName:
    title: str
    mod_id: Optional[int]
    version: str = ""
    ts: Optional[int] = None          # метка загрузки Nexus (unix), если есть в имени
    copy_index: Optional[int] = None  # «(1)» от браузера
    rejected_id: Optional[int] = None  # число из имени, отброшенное как «не Mod ID» (вне диапазона)


# Нестандартный формат: «Название 12345 v1.2 2024-01-01T12-30-00Z hash»
VORTEX_PATTERN = re.compile(
    r"^(?P<title>.+?)\s+(?P<id>\d{3,7})\s+(?P<ver>v?[\d\.\w\-]+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2}T[\d\-]+Z?)\s*(?P<hash>\w+)?$",
    re.IGNORECASE,
)
VORTEX_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})")
COPY_SUFFIX = re.compile(r"\s*\((\d{1,3})\)\s*$")
TS_TAIL = re.compile(r"-(1\d{9})$")
ID_TOKEN = re.compile(r"^\d{2,7}$")
VERSION_START = re.compile(r"^v?\d", re.IGNORECASE)
# Компонент версии: 1, 37, v2, 1a, 1.2 или год вида 2024; слова — пре-релизы.
# «patch» и т. п. сюда нарочно не входят: это часть названия, а не версии.
VERSION_TOKEN = re.compile(
    r"^(?:v?\d{1,3}(?:\.\d{1,3})*[a-z]{0,2}|(?:19|20)[0-3]\d|alpha|beta|rc\d*|hotfix|final|pre)$",
    re.IGNORECASE,
)
JUNK_WORDS = re.compile(r"\b(мод|версия|для|патч|киберпанк|обновление|скачать|v\d+)\b", re.IGNORECASE)
MAX_VERSION_TOKENS = 6
PRERELEASE_RANK = {"alpha": -3, "beta": -2, "pre": -2, "rc": -1}


# Реальные Mod ID Cyberpunk 2077 на Nexus лежат в диапазоне 1..MAX_MOD_ID. Всё, что больше, —
# это File ID, дата или чужой номер (например, «Виртуальная мастерская-168466.zip»), и ссылка
# вида /mods/168466 даст 404. Потолок растёт вместе с Nexus: при необходимости задайте
# "max_mod_id" в config.json.
MAX_MOD_ID = 35000


def is_valid_mod_id(n: Optional[int]) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= MAX_MOD_ID


def _id_fields(n: int) -> dict:
    """Поля ParsedName для числа-кандидата: подтверждённый ID или отброшенный."""
    ok = is_valid_mod_id(n)
    return {"mod_id": n if ok else None, "rejected_id": None if ok else n}


def _looks_like_year(n: int) -> bool:
    # 2077 — название игры, в именах мусорный «кандидат» №1
    return 2000 <= n <= 2039 or n == 2077


def _tokenize(text: str, sep: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in re.finditer(rf"[^{sep}]+", text)]


def _pick_id_index(tokens: list[str], *, anchored: bool, strict: bool = True) -> Optional[int]:
    """Выбирает токен-ID среди токенов имени.

    strict=True — кандидат обязан быть реальным Mod ID (1..MAX_MOD_ID). strict=False
    нужен только чтобы найти «ложное» число (File ID) и вырезать его из названия;
    mod_id для такого числа всегда None.

    Кандидат — число, за которым идёт «хвост» из компонентов версии. При
    anchored=True хвост обязателен (за ним стоит timestamp Nexus), и годы
    допустимы — они лишь теряют приоритет. Без якоря годы и короткие числа
    отбрасываются полностью.
    """
    ranked: list[tuple[tuple[bool, bool, int], int]] = []
    for i, tok in enumerate(tokens):
        if not ID_TOKEN.match(tok):
            continue
        value = int(tok)
        if strict and not is_valid_mod_id(value):
            continue
        suffix = tokens[i + 1:]
        if anchored:
            if not (1 <= len(suffix) <= MAX_VERSION_TOKENS) or not VERSION_START.match(suffix[0]):
                continue
        elif _looks_like_year(value) or len(tok) < 3:
            continue
        if not all(VERSION_TOKEN.match(t) for t in suffix):
            continue
        ranked.append(((_looks_like_year(value), len(tok) < 3, i), i))
    return min(ranked)[1] if ranked else None


def _format_version(tokens: Iterable[str]) -> str:
    out = ""
    for i, tok in enumerate(tokens):
        if i == 0 and re.match(r"^v\d", tok, re.IGNORECASE):
            tok = tok[1:]
        sep = "" if i == 0 else ("." if tok[:1].isdigit() else "-")
        out += sep + tok
    return out


def _vortex_ts(date: str) -> Optional[int]:
    m = VORTEX_DATE.match(date)
    if not m:
        return None
    try:
        return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))  # type: ignore[arg-type]
    except (ValueError, OverflowError):
        return None


def _parse_vortex_name(stem: str, copy_index: Optional[int]) -> Optional[ParsedName]:
    match = VORTEX_PATTERN.match(stem)
    if match:
        return ParsedName(
            match.group("title").strip(" -_"),
            version=_format_version([match.group("ver")]),
            ts=_vortex_ts(match.group("date")),
            copy_index=copy_index,
            **_id_fields(int(match.group("id"))),
        )
    return None


def _parse_timestamped_nexus_name(stem: str, copy_index: Optional[int]) -> Optional[ParsedName]:
    # Стандартное имя Nexus: <название>-<id>-<версия через дефисы>-<timestamp>.
    # Timestamp — надёжный якорь, фильтр «годов» здесь не нужен.
    match = TS_TAIL.search(stem)
    if not match:
        return None
    head = stem[: match.start()]
    tokens = _tokenize(head, "-")
    words = [token for token, _ in tokens]
    index = _pick_id_index(words, anchored=True)
    if index is None:
        index = _pick_id_index(words, anchored=True, strict=False)
    if index is None:
        return None
    return ParsedName(
        head[: tokens[index][1]].strip(" -_"),
        version=_format_version(words[index + 1:]),
        ts=int(match.group(1)),
        copy_index=copy_index,
        **_id_fields(int(words[index])),
    )


def _parse_manual_name(stem: str, copy_index: Optional[int]) -> Optional[ParsedName]:
    toks = _tokenize(stem, r"\-_ ")
    words = [t for t, _ in toks]
    idx = _pick_id_index(words, anchored=False)
    if idx is None:
        idx = _pick_id_index(words, anchored=False, strict=False)
    if idx is None:
        return None
    return ParsedName(
        stem[: toks[idx][1]].strip(" -_"),
        version=_format_version(words[idx + 1:]),
        copy_index=copy_index,
        **_id_fields(int(words[idx])),
    )


def _clean_unparsed_name(stem: str, copy_index: Optional[int]) -> ParsedName:
    t = JUNK_WORDS.sub("", stem)
    t = re.sub(r"[«»\"'\[\]\(\)]", " ", t)
    t = re.sub(r"[-_.]+", " ", t)
    return ParsedName(" ".join(t.split()) or stem, None, "", None, copy_index)


def extract_mod_meta(stem: str) -> ParsedName:
    """Разбирает имя архива (без расширения) на название, ID, версию и timestamp."""
    copy_index: Optional[int] = None
    match = COPY_SUFFIX.search(stem)
    if match:
        copy_index = int(match.group(1))
        stem = stem[: match.start()]
    stem = stem.strip()

    return (
        _parse_vortex_name(stem, copy_index)
        or _parse_timestamped_nexus_name(stem, copy_index)
        or _parse_manual_name(stem, copy_index)
        or _clean_unparsed_name(stem, copy_index)
    )


def norm_title(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.lower()).strip()


def version_key(version: str) -> Optional[tuple[tuple[int, ...], int]]:
    """Ключ для сравнения версий: 1.37.1 > 1.9; релиз > rc > beta > alpha."""
    nums: list[int] = []
    rank = 0
    for part in re.split(r"[.\-_]", version.lower()):
        if not part:
            continue
        if part.isdigit():
            nums.append(int(part))
            continue
        m = re.match(r"^v?(\d+)", part)
        if m:
            nums.append(int(m.group(1)))
            continue
        word = re.match(r"^[a-z]+", part)
        if word and word.group(0) in PRERELEASE_RANK:
            rank = PRERELEASE_RANK[word.group(0)]
    while nums and nums[-1] == 0:
        nums.pop()
    return (tuple(nums), rank) if nums else None


def compare_freshness(a: ParsedName, b: ParsedName) -> Optional[int]:
    """-1: a старее b, 1: a новее b, 0: одинаково, None: сравнить нечем.

    Время файловой системы (st_mtime) намеренно не используется: оно меняется
    при копировании и переносе. Сравниваем timestamp загрузки Nexus, затем версию.
    """
    if a.ts and b.ts:
        return (a.ts > b.ts) - (a.ts < b.ts)
    ka, kb = version_key(a.version), version_key(b.version)
    if ka and kb:
        return (ka > kb) - (ka < kb)
    return None


# ==================== ДАННЫЕ ====================

@dataclass
class Archive:
    id: int
    source: str            # "downloads" | "vortex"
    path: Path
    size: int
    mtime: float
    meta: ParsedName
    sha256: str = ""
    verdict: str = KEEP
    note: str = ""
    keeper: Optional[int] = None   # id архива, который остаётся вместо этого

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def is_candidate(self) -> bool:
        return self.source == "downloads" and self.verdict in (EXACT_DUP, OLDER)


@dataclass
class ModRow:
    mod_id: Optional[int]
    title: str
    files: list[Archive]
    danger: str
    status: str
    actions: list[str]
    cats: set[str]
    grouping_reason: str


@dataclass(frozen=True)
class GameModEntry:
    section: str
    name: str
    path: Path
    file_count: int
    size: int


GAME_MOD_ROOTS = (
    ("archive/pc/mod", Path("archive/pc/mod")),
    ("archive/pc/patch", Path("archive/pc/patch")),
    ("r6/scripts", Path("r6/scripts")),
    ("r6/tweaks", Path("r6/tweaks")),
    ("red4ext/plugins", Path("red4ext/plugins")),
    ("bin/x64/plugins", Path("bin/x64/plugins")),
    ("mods (REDmod)", Path("mods")),
)


def scan_game_mods(game_path: Optional[Path]) -> list[GameModEntry]:
    """Inventory entries in known Cyberpunk mod deployment folders."""
    if game_path is None or not game_path.is_dir():
        return []

    entries: list[GameModEntry] = []
    for section, relative_root in GAME_MOD_ROOTS:
        root = game_path / relative_root
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        except OSError as exc:
            log.warning("Не удалось прочитать папку модов %s: %s", root, exc)
            continue

        for child in children:
            if child.is_symlink():
                continue
            files: list[Path] = []
            if child.is_file():
                files.append(child)
            elif child.is_dir():
                for current, dirs, names in os.walk(child, onerror=lambda exc: log.warning(
                        "Не удалось прочитать папку модов %s: %s", exc.filename, exc.strerror)):
                    dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
                    dirs.sort(key=str.casefold)
                    files.extend(
                        path for name in names
                        if not (path := Path(current) / name).is_symlink()
                    )
            else:
                continue

            total_size = 0
            readable_files = 0
            for path in files:
                try:
                    total_size += path.stat().st_size
                    readable_files += 1
                except OSError as exc:
                    log.warning("Не удалось прочитать файл мода %s: %s", path, exc)
            if readable_files:
                entries.append(GameModEntry(
                    section=section,
                    name=child.name,
                    path=child,
                    file_count=readable_files,
                    size=total_size,
                ))
    return entries


def scan_folder(folder: Path, source: str, start_id: int = 0,
                exclude: Iterable[Path] = ()) -> list[Archive]:
    items: list[Archive] = []
    skip = {os.path.normcase(os.path.abspath(p)) for p in exclude}

    def on_error(err: OSError) -> None:
        log.warning("Не удалось прочитать папку %s: %s", err.filename, err.strerror)

    for root, dirs, names in os.walk(folder, onerror=on_error):
        dirs[:] = [d for d in dirs
                   if d != QUARANTINE_DIRNAME
                   and os.path.normcase(os.path.abspath(os.path.join(root, d))) not in skip]
        dirs.sort(key=str.casefold)
        for name in sorted(names, key=str.casefold):
            p = Path(root) / name
            if p.suffix.lower() not in EXTENSIONS:
                continue
            try:
                st = p.stat()
            except OSError as exc:
                log.warning("Пропускаю %s: %s", p, exc)
                continue
            items.append(Archive(
                id=start_id + len(items), source=source, path=Path(os.path.abspath(p)),
                size=st.st_size, mtime=st.st_mtime, meta=extract_mod_meta(p.stem),
            ))
    return items


def sha256_file(path: Path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def check_game_core(game_path: Optional[Path]) -> dict[str, bool]:
    if game_path is None or not game_path.is_dir():
        return {}
    return {name: any((game_path / rel).exists() for rel in rels)
            for name, rels in CORE_FRAMEWORKS.items()}


def parse_library_folders(vdf_text: str) -> list[Path]:
    return [Path(p.replace("\\\\", "\\"))
            for p in re.findall(r'"path"\s+"([^"]+)"', vdf_text)]


def find_cyberpunk_install() -> Optional[Path]:
    """Ищет игру в библиотеках Steam (реестр + libraryfolders.vdf)."""
    roots: list[Path] = []
    try:
        import winreg  # только Windows
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
    except (ImportError, OSError):
        pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]) / "Steam")
    for root in roots:
        libs = [root]
        try:
            libs += parse_library_folders((root / "steamapps" / "libraryfolders.vdf")
                                          .read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
        for lib in libs:
            cand = lib / "steamapps" / "common" / "Cyberpunk 2077"
            if cand.is_dir():
                return cand
    return None


# ==================== АНАЛИЗ ====================

def mark_exact_duplicates(archives: list[Archive]) -> None:
    """Точные копии: одинаковые размер и SHA-256, независимо от имён файлов.

    Хешируются только файлы, у которых есть «ровесник» по размеру.
    Файлы из Vortex никогда не становятся кандидатами на удаление.
    """
    by_size: dict[int, list[Archive]] = defaultdict(list)
    for a in archives:
        if a.size > 0:
            by_size[a.size].append(a)
    suspects = [a for group in by_size.values() if len(group) > 1 for a in group]
    if suspects:
        total = sum(a.size for a in suspects) / 1024 ** 3
        log.info("[*] Считаю SHA-256 для %d файлов с одинаковым размером (%.1f ГБ)...",
                 len(suspects), total)
    for n, a in enumerate(suspects, 1):
        try:
            a.sha256 = sha256_file(a.path)
        except OSError as exc:
            log.warning("Не удалось прочитать %s: %s", a.path, exc)
            continue
        if n % 20 == 0:
            log.info("    %d / %d", n, len(suspects))

    clusters: dict[tuple[int, str], list[Archive]] = defaultdict(list)
    for a in suspects:
        if a.sha256:
            clusters[(a.size, a.sha256)].append(a)

    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        vortex = sorted((a for a in cluster if a.source == "vortex"), key=lambda a: str(a.path))
        downloads = [a for a in cluster if a.source == "downloads"]
        if vortex:
            keeper, doomed = vortex[0], downloads
        else:
            downloads.sort(key=lambda a: (a.meta.copy_index or 0, len(a.name), a.mtime, str(a.path)))
            keeper, doomed = downloads[0], downloads[1:]
        for a in doomed:
            a.verdict, a.keeper = EXACT_DUP, keeper.id
            where = "в Vortex" if keeper.source == "vortex" else "в Downloads"
            a.note = f"Идентичен (SHA-256) файлу {keeper.name} {where}"


def mark_versions(archives: list[Archive]) -> None:
    """Старые версии одного файла мода: ID + название файла + ts/версия."""
    by_id = {a.id: a for a in archives}
    groups: dict[tuple[int, str], list[Archive]] = defaultdict(list)
    for a in archives:
        if a.meta.mod_id is not None:
            groups[(a.meta.mod_id, norm_title(a.meta.title))].append(a)

    for items in groups.values():
        if len(items) < 2:
            continue

        def newer_than(a: Archive) -> list[Archive]:
            return [b for b in items if b is not a and compare_freshness(a.meta, b.meta) == -1]

        # 1-й проход: помечаем всех, у кого есть более новый файл.
        marked = [a for a in items
                  if a.source == "downloads" and a.verdict == KEEP and newer_than(a)]
        for a in marked:
            a.verdict = OLDER
        # 2-й проход: остающийся файл — самый свежий из не помеченных
        # (для точных копий — тот, на кого они ссылаются).
        for a in marked:
            finals: list[tuple[Archive, Archive]] = []
            for b in newer_than(a):
                if b.verdict == EXACT_DUP and b.keeper is not None:
                    finals.append((b, by_id[b.keeper]))
                elif b.verdict != OLDER:
                    finals.append((b, b))
            if not finals:
                a.verdict = KEEP
                continue
            best = finals[0]
            for cand in finals[1:]:
                if compare_freshness(best[0].meta, cand[0].meta) == -1:
                    best = cand
            a.keeper = best[1].id
            ver = best[0].meta.version or "?"
            a.note = f"Есть новее: {best[1].name} (v{ver}, {best[1].source})"

        # Информационные пометки Downloads новее Vortex.
        for a in items:
            if a.source != "downloads" or a.verdict != KEEP:
                continue
            for b in items:
                if b.source == "vortex" and b.verdict == KEEP and compare_freshness(a.meta, b.meta) == 1:
                    a.verdict = NEWER
                    a.note = f"Новее, чем в Vortex (v{b.meta.version or '?'}) — обновите мод в Vortex"
                    b.verdict = STALE
                    b.note = f"В Downloads есть более новый архив: {a.name}"
                    break


def common_title(files: list[Archive]) -> str:
    uniq: dict[str, str] = {}
    for f in files:
        uniq.setdefault(norm_title(f.meta.title), f.meta.title)
    titles = list(uniq.values())
    if len(titles) == 1:
        return titles[0]
    words = [re.findall(r"[^\s\-_]+", t) for t in titles]
    prefix = os.path.commonprefix([[w.lower() for w in ws] for ws in words])
    if prefix:
        return " ".join(words[0][: len(prefix)])
    return min(titles, key=len)


def fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


# ---- Очистка названий и известные моды ----

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_BRACKETS = re.compile(r"[\(\[\{][^)\]}]*[\)\]\}]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*")
_DASH_VERSION = re.compile(r"(?<=[A-Za-z0-9])(?:-\d+)+(?![A-Za-z0-9])")
_EXPLICIT_VERSION = re.compile(r"^v\d[\w.\-]*$", re.IGNORECASE)      # v1, v2.3, v1-0
_VERSIONISH = re.compile(r"^v?\d+(?:[-.]\d+)*[a-z]{0,2}$", re.IGNORECASE)  # 1, 1.37, 1-0, 2a
TITLE_NOISE = {"cyberpunk", "2077", "cp2077"}
TITLE_SUFFIXES = {"mod", "mods", "patch", "fix", "fixed", "hotfix", "update", "updated",
                  "version", "ver", "release", "final", "beta", "alpha", "rc", "pre"}
MAX_SEARCH_WORDS = 3


def _title_words(text: object) -> list[str]:
    """Латинские слова названия без версий, «Cyberpunk 2077», скобок и хвостовых суффиксов."""
    s = _BRACKETS.sub(" ", str(text or "").replace("’", "'"))
    s = _DASH_VERSION.sub(" ", s)   # «Name-1-0» → «Name»
    words = [w for w in _WORD.findall(s)
             if w.lower() not in TITLE_NOISE and not _EXPLICIT_VERSION.match(w)]
    while words and (words[-1].lower() in TITLE_SUFFIXES or _VERSIONISH.match(words[-1])):
        words.pop()
    return words


def clean_search_title(text: object, max_words: int = MAX_SEARCH_WORDS) -> str:
    """Чистое английское имя из 1–3 слов для поиска. Текст с русскими буквами отвергается целиком."""
    s = str(text or "")
    if CYRILLIC.search(s):
        return ""
    return " ".join(_title_words(s)[:max_words])


def _known_key(text: object) -> str:
    return re.sub(r"[^a-z0-9]", "", "".join(_title_words(text)).lower())


_KNOWN_INDEX: dict[str, int] = {_known_key(name): mid for name, mid in KNOWN_MOD_IDS.items()}


def known_mod_id(title: object) -> Optional[int]:
    """Mod ID из KNOWN_MOD_IDS, если название — это именно известный мод (иначе None)."""
    s = str(title or "")
    if not s or CYRILLIC.search(s):   # смешанные названия («… для TweakXL») могут быть переводами/патчами
        return None
    key = _known_key(s)
    return _KNOWN_INDEX.get(key) if key else None


def resolve_mod_id(a: Archive, title_ids: Optional[dict[str, int]] = None) -> Optional[int]:
    """ID из имени файла → известный мод по имени файла → известный мод по ответу ИИ."""
    if a.meta.mod_id is not None:
        return a.meta.mod_id
    mid = known_mod_id(a.meta.title)
    if mid is None and title_ids:
        mid = title_ids.get(norm_title(a.meta.title))
    return mid


def build_rows(archives: list[Archive], blacklist: dict[int, str],
               title_ids: Optional[dict[str, int]] = None) -> list[ModRow]:
    """title_ids — {norm_title: Mod ID}, найденные по английскому названию от ИИ (см. known_ids_from_ai)."""
    buckets: dict[object, list[Archive]] = defaultdict(list)
    for a in archives:
        mid = resolve_mod_id(a, title_ids)
        key: object = mid if mid is not None else f"t:{norm_title(a.meta.title)}"
        buckets[key].append(a)

    rows: list[ModRow] = []
    for key, files in buckets.items():
        files.sort(key=lambda a: (a.source != "downloads", a.name.lower()))
        mod_id = key if isinstance(key, int) else None
        title = common_title(files)
        if mod_id is None:
            grouping_reason = "очищенное название архива (ID не распознан)"
        else:
            evidence = set()
            if any(f.meta.mod_id == mod_id for f in files):
                evidence.add("ID в имени архива")
            if any(known_mod_id(f.meta.title) == mod_id for f in files):
                evidence.add("встроенный словарь имён")
            if title_ids and title_ids.get(norm_title(title)) == mod_id:
                evidence.add("название ИИ совпало со встроенным словарём")
            grouping_reason = f"Nexus ID #{mod_id} ({', '.join(sorted(evidence)) or 'одинаковый ID'})"
        danger = blacklist.get(mod_id, "") if mod_id is not None else ""
        has_dl = any(f.source == "downloads" for f in files)
        has_vx = any(f.source == "vortex" for f in files)
        exact = [f for f in files if f.verdict == EXACT_DUP]
        older = [f for f in files if f.verdict == OLDER]
        newer = [f for f in files if f.verdict == NEWER]

        cats = {"all"}
        actions: list[str] = []
        status = ""
        if danger:
            cats.add("danger")
            status = "danger"
        if exact:
            cats.add("dup")
            actions.append(f"Точных копий в Downloads: {len(exact)} ({fmt_size(sum(f.size for f in exact))}) — можно удалить")
            status = status or "dup"
        if older:
            cats.add("old")
            actions.append(f"Старых версий в Downloads: {len(older)} — есть более новые архивы")
            status = status or "old"
        if newer:
            cats.add("update")
            actions.append("В Downloads версия новее, чем в Vortex — обновите мод в Vortex, архив не удаляйте")
            status = status or "update"
        cats.update(source_categories(f.source for f in files))
        if has_dl:
            cats.add("nexus_downloads")
        if not actions:
            if has_dl and has_vx:
                actions.append("Разные файлы одного мода — не дубликаты")
                status = status or "both"
            elif has_vx:
                actions.append("Архив найден только в папке загрузок Vortex")
                status = status or "vortex"
            else:
                actions.append("Архив найден только в Downloads; способ установки не определён")
                status = status or "local"
        if mod_id is None:
            rejected = sorted({f.meta.rejected_id for f in files if f.meta.rejected_id is not None})
            if rejected:
                actions.append(
                    f"Число {', '.join(map(str, rejected))} из имени файла — не Mod ID "
                    f"(вне диапазона 1–{MAX_MOD_ID}, скорее всего File ID): ссылка только через поиск"
                )
        rows.append(ModRow(mod_id, title, files, danger, status, actions, cats, grouping_reason))

    status_order = {"danger": 0, "dup": 1, "old": 2, "update": 3, "both": 4, "vortex": 5, "local": 6}
    rows.sort(key=lambda r: (status_order[r.status], r.title.lower()))
    return rows


def load_custom_blacklist(path: Path = BLACKLIST_PATH) -> dict[int, str]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("blacklist.json не прочитан (%s) — использую встроенный список", exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("blacklist.json должен содержать JSON-объект — использую встроенный список")
        return {}

    result: dict[int, str] = {}
    for key, val in raw.items():
        try:
            mod_id = int(key)
        except (TypeError, ValueError):
            log.warning("Пропускаю некорректный Mod ID в blacklist.json: %s", key)
            continue
        if isinstance(val, str):
            result[mod_id] = val
        elif isinstance(val, dict):
            extra = ", ".join(str(val[k]) for k in ("game_version", "date") if val.get(k))
            result[mod_id] = f"{val.get('reason', 'в чёрном списке')}" + (f" [{extra}]" if extra else "")
        else:
            log.warning("Пропускаю некорректную запись blacklist.json для Mod ID %s", mod_id)
    return result


def load_blacklist(path: Path = BLACKLIST_PATH) -> dict[int, str]:
    result = dict(OUTDATED_OR_DANGEROUS)
    result.update(load_custom_blacklist(path))
    return result


# ==================== ИИ-ПОМОЩНИК (OpenRouter) ====================
#
# Нейросеть угадывает английское название и поисковый запрос по имени архива
# (в том числе по русскому) и по его составу (.archive, .reds, .yaml, папки r6/scripts).
# Mod ID от неё принимается только в диапазоне 1..MAX_MOD_ID и не раньше проверки
# человеком: он НЕ становится ModRow.mod_id (группировка, чёрный список), а показывается
# отдельной ссылкой «≈ #ID ИИ» рядом с поиском Nexus. Фактические квоты OpenRouter зависят
# от аккаунта и модели. Приложение отдельно ограничивает
# собственные попытки, а ответы кэшируются в ai_cache.json.

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
AI_CACHE_PATH = SCRIPT_DIR / "ai_cache.json"
# Бесплатные модели периодически меняются: актуальный список — `python nexus_links.py ai-models`.
DEFAULT_AI_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
]
AI_BATCH_SIZE = 20          # модов в одном запросе
AI_TIMEOUT = 120
AI_DEFAULT_MAX_REQUESTS = 8  # внутренний максимум пачек за один запуск

AI_SYSTEM_PROMPT_TEMPLATE = (
    "Ты определяешь настоящее английское название мода для Cyberpunk 2077 на Nexus Mods по названию "
    "скачанного архива (оно может быть на русском, английском или вперемешку, с мусором в имени файла) "
    "и по составу архива: имена файлов .archive, .reds, .yaml, .xl, .lua и папки в r6/scripts. "
    "Вход — массив объектов {i, title, files, contents}; contents — список имён внутри архива (может быть пустым). "
    "Для каждого элемента верни JSON-объект с полями: "
    "i (число, как во входе), "
    "clean_title (оригинальное английское название мода на Nexus; пустая строка, если не уверен), "
    "mod_id (число или null), "
    "search_query (запрос для поиска на Nexus Mods), "
    "ru (короткое название или описание по-русски), "
    "kind (категория по-русски одним-двумя словами: одежда, оружие, транспорт, внешность, геймплей, "
    "интерфейс, графика, звук, фреймворк, скрипты, прочее), "
    "conf (high, medium или low — насколько ты уверен в clean_title). "
    "СТРОГИЕ ПРАВИЛА. "
    "1) Если название переведено на русский (например, «Виртуальная мастерская»), восстанови оригинальное "
    "имя мода на Nexus (Virtual Atelier). Имена файлов и папок внутри архива (особенно в r6/scripts) часто "
    "совпадают с оригинальным названием — используй их как подсказку. "
    "2) Mod ID мода для Cyberpunk 2077 — число от 1 до {max_id}. Указывать ID больше {max_id} ЗАПРЕЩЕНО. "
    "Если ты не уверен в точном ID на 100% — верни mod_id: null (так будет в большинстве случаев). "
    "Числа в имени файла (например, 168466) — это обычно File ID или дата, а не Mod ID: не копируй их. "
    "Никогда не выдумывай ссылки и имена авторов. "
    "3) search_query — чистый поисковый запрос: 2-4 английских ключевых слова, без кавычек и спецсимволов, "
    "без слова Cyberpunk. Если смысл неясен — conf=low, clean_title пустой, search_query собери из слов "
    "названия (русские слова переведи). "
    "Пример элемента ответа: "
    '{"i": 0, "clean_title": "Virtual Atelier", "mod_id": null, "search_query": "Virtual Atelier", '
    '"ru": "Виртуальная мастерская", "kind": "одежда", "conf": "high"}. '
    "Ответ — только JSON-массив таких объектов, без пояснений и без markdown."
)


def ai_system_prompt() -> str:
    return AI_SYSTEM_PROMPT_TEMPLATE.replace("{max_id}", str(MAX_MOD_ID))


@dataclass
class AIInfo:
    en: str = ""
    ru: str = ""
    query: str = ""
    kind: str = ""
    conf: str = "low"
    mod_id: Optional[int] = None   # догадка ИИ, всегда в диапазоне 1..MAX_MOD_ID, не подтверждена


class AIError(Exception):
    def __init__(self, message: str, status: int = 0, body: str = "", fatal: bool = False):
        super().__init__(message)
        self.status, self.body, self.fatal = status, body, fatal


def _http_json(url: str, key: str = "", payload: Optional[dict] = None) -> dict:
    headers = {"Content-Type": "application/json", "X-Title": "Cyberpunk Mod Auditor"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise AIError(f"HTTP {exc.code}: {body[:200]}", exc.code, body) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise AIError(f"сеть/ответ: {exc}") from exc


def _parse_ai_json(text: str) -> Optional[list[dict]]:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b <= a:
        return None
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else None


def _ai_ask(
    models: list[str],
    key: str,
    user_text: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 8000,
) -> tuple[str, list[dict]]:
    """Пробует модели по очереди. Возвращает (модель, список объектов)."""
    last: Optional[AIError] = None
    for model in models:
        if not reserve_request():
            raise AIError(
                "Достигнут внутренний лимит приложения на запросы OpenRouter "
                "за запуск или UTC-сутки.",
                fatal=True,
            )
        wait_for_request_slot()
        try:
            data = _http_json(f"{OPENROUTER_BASE}/chat/completions", key, {
                "model": model,
                "messages": [{"role": "system", "content": system_prompt or ai_system_prompt()},
                             {"role": "user", "content": user_text}],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            })
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            items = _parse_ai_json(text)
            if items is not None:
                return model, items
            last = AIError(f"{model}: ответ не похож на JSON")
        except AIError as exc:
            last = exc
            if exc.status in (401, 402, 403):
                block_remaining_requests_this_run()
                raise AIError(f"OpenRouter отклонил ключ/баланс ({exc.status}). Проверьте ключ.",
                              exc.status, exc.body, fatal=True) from exc
            if exc.status == 429 and re.search(r"per[- ]?day|daily", exc.body, re.I):
                block_remaining_requests_this_run()
                raise AIError("OpenRouter сообщил об исчерпании суточной квоты. "
                              "Результаты сохранены, попробуйте позже.",
                              429, exc.body, fatal=True) from exc
        if model != models[-1]:
            log.info("    модель %s недоступна, сразу переключаюсь на следующую", model)
    raise last or AIError("нет доступных моделей")


def _clean_str(value: object, limit: int = 120) -> str:
    s = re.sub(r"[\x00-\x1f]+", " ", str(value or ""))
    return " ".join(s.split())[:limit]


def _to_ai_mod_id(value: object) -> Optional[int]:
    """ID от ИИ принимается только как целое число в диапазоне 1..MAX_MOD_ID, иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and is_valid_mod_id(value) else None


def _clean_query(value: object) -> str:
    q = re.sub(r"[^\w\s\-]+", " ", str(value or ""))   # без кавычек и спецсимволов
    return _clean_str(q, 80)


def _to_info(obj: dict) -> AIInfo:
    conf = _clean_str(obj.get("conf"), 10).lower()
    conf = conf if conf in ("high", "medium", "low") else "low"
    en = _clean_str(obj.get("clean_title") or obj.get("en"), 100)
    query = _clean_query(obj.get("search_query") or obj.get("q") or obj.get("query")) or _clean_query(en)
    # Низкая уверенность + ID — противоречие (правило «только если уверен на 100%»): ID отбрасываем.
    mod_id = None if conf == "low" else _to_ai_mod_id(obj.get("mod_id"))
    return AIInfo(
        en=en, ru=_clean_str(obj.get("ru"), 100), query=query,
        kind=_clean_str(obj.get("kind"), 40), conf=conf, mod_id=mod_id,
    )


# Что из состава архива отправляется ИИ: папки r6/scripts/<имя> и имена файлов этих типов.
DIGEST_EXT = {".archive", ".reds", ".yaml", ".yml", ".xl", ".lua", ".dll", ".asi"}
DIGEST_LIMIT = 15
DIGEST_FOLDERS = 6
MEMBERS_SCAN_LIMIT = 5000


def list_archive_members(path: Path) -> list[str]:
    """Имена файлов внутри архива. zip — штатно; 7z/rar — если установлены py7zr/rarfile."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                return z.namelist()[:MEMBERS_SCAN_LIMIT]
        if suffix == ".7z":
            import py7zr  # type: ignore[import-not-found]
            with py7zr.SevenZipFile(path) as z:
                return list(z.getnames())[:MEMBERS_SCAN_LIMIT]
        if suffix == ".rar":
            import rarfile  # type: ignore[import-not-found]
            with rarfile.RarFile(path) as r:
                return r.namelist()[:MEMBERS_SCAN_LIMIT]
    except Exception as exc:  # noqa: BLE001 — битый/запароленный архив или нет библиотеки
        log.debug("[AI] состав %s не прочитан: %s", path.name, exc)
    return []


def archive_digest(paths: Iterable[Path]) -> list[str]:
    """Короткая выжимка состава первого читаемого архива (без путей на диске пользователя)."""
    for path in paths:
        members = list_archive_members(path)
        if not members:
            continue
        folders: list[str] = []
        files: list[str] = []
        for raw in members:
            parts = [p for p in raw.replace("\\", "/").split("/") if p]
            if not parts:
                continue
            if len(parts) > 3 and parts[0].lower() == "r6" and parts[1].lower() == "scripts":
                item = "r6/scripts/" + parts[2]       # папка мода внутри r6/scripts
                if item not in folders:
                    folders.append(item)
            if not raw.endswith("/") and Path(parts[-1]).suffix.lower() in DIGEST_EXT:
                if parts[-1] not in files:
                    files.append(parts[-1])
        head = folders[:DIGEST_FOLDERS]
        return [_clean_str(x, 80) for x in (head + files)[:DIGEST_LIMIT]]
    return []


def load_ai_cache(path: Path = AI_CACHE_PATH) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("[AI] кэш не прочитан (%s): %s", path, exc)
        return {}
    try:
        items = raw.get("items", {})
        if isinstance(items, dict):
            return items
    except AttributeError:
        pass
    log.warning("[AI] кэш имеет неверный формат (%s); начинаю с пустого кэша", path)
    return {}


def save_ai_cache(items: dict[str, dict], path: Path = AI_CACHE_PATH) -> None:
    try:
        path.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("[AI] кэш не сохранён (%s): %s", path, exc)
    else:
        log.info("[AI] кэш сохранён: %s (%d записей)", path, len(items))


def run_ai(rows: list[ModRow], settings: "Settings", args: argparse.Namespace,
           online: bool) -> dict[str, AIInfo]:
    """Возвращает {norm_title: AIInfo}. Без online — только то, что уже лежит в кэше."""
    cache = load_ai_cache()
    result: dict[str, AIInfo] = {}
    for r in rows:
        entry = cache.get(norm_title(r.title))
        if isinstance(entry, dict) and isinstance(entry.get("info"), dict):
            i = entry["info"]
            result[norm_title(r.title)] = AIInfo(
                _clean_str(i.get("en")), _clean_str(i.get("ru")), _clean_str(i.get("query"), 80),
                _clean_str(i.get("kind"), 40), i.get("conf") if i.get("conf") in ("high", "medium", "low") else "low",
                _to_ai_mod_id(i.get("mod_id")))
    if not online:
        return result

    try:
        cfg = settings.cfg or {}
        ai_cfg = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        key = key or str(ai_cfg.get("api_key") or "").strip()
        if not key or key.upper().startswith(("YOUR_", "YOUR-")):
            if args.ai_auto and not args.ai and not args.ai_all:
                log.info("[AI] ключ OpenRouter не введён; запускаю сканирование без ИИ.")
            else:
                log.warning("[AI] нет ключа OpenRouter: задайте переменную OPENROUTER_API_KEY "
                            "или \"ai\": {\"api_key\": \"...\"} в config.json. Показываю только кэш.")
            return result

        configured_models = ai_cfg.get("models")
        if not isinstance(configured_models, list) or not configured_models:
            configured_models = DEFAULT_AI_MODELS
        models_value = [args.ai_model] if args.ai_model else configured_models
        models = [str(model) for model in models_value if str(model).strip()]
        if not models:
            log.error("[AI] Список моделей OpenRouter пуст.")
            return result

        max_requests = getattr(args, "ai_max_requests", None)
        if max_requests is None:
            configured_max_requests = ai_cfg.get("max_requests", AI_DEFAULT_MAX_REQUESTS)
            if (not isinstance(configured_max_requests, int)
                    or isinstance(configured_max_requests, bool)
                    or configured_max_requests < 0):
                log.warning("[AI] Некорректный ai.max_requests; использую значение по умолчанию: %d",
                            AI_DEFAULT_MAX_REQUESTS)
                configured_max_requests = AI_DEFAULT_MAX_REQUESTS
            max_requests = configured_max_requests

        # Сюда попадают все моды без подтверждённого ID — в том числе те, чей «ID» из имени
        # файла вышел за диапазон 1..MAX_MOD_ID и был отброшен при разборе имени.
        wanted: dict[str, tuple[str, list[str], list[Path]]] = {}
        for r in rows:
            if r.mod_id is not None and not args.ai_all:
                continue
            k = norm_title(r.title)
            if k and k not in wanted:
                wanted[k] = (r.title, [f.name for f in r.files][:2], [f.path for f in r.files][:3])
        if not wanted:
            log.info("[AI] нет модов без распознанного ID; сетевой анализ и запись в кэш не требуются")
            return result
        todo = [k for k in wanted if args.ai_refresh or k not in result]
        if not todo:
            log.info("[AI] всё уже в кэше (%d), запросы не нужны", len(wanted))
            return result

        log.info("[AI] модов к анализу: %d, модель: %s", len(todo), models[0])
        log.info("[AI] уходят только названия модов, имена файлов и имена внутри архивов "
                 "(.archive/.reds/.yaml, r6/scripts) — без путей и имени пользователя")
        batches = [todo[i:i + AI_BATCH_SIZE] for i in range(0, len(todo), AI_BATCH_SIZE)]
        if len(batches) > max_requests:
            log.info("[AI] за запуск обработаю %d из %d пачек (--ai-max-requests), остальное — при следующем запуске",
                     max_requests, len(batches))
            batches = batches[:max_requests]

        for n, batch in enumerate(batches, 1):
            payload = [{"i": i, "title": wanted[k][0], "files": wanted[k][1],
                        "contents": archive_digest(wanted[k][2])} for i, k in enumerate(batch)]
            log.info("[AI] пачка %d/%d (%d шт.)…", n, len(batches), len(batch))
            try:
                model, items = _ai_ask(models, key, "Определи моды:\n" + json.dumps(payload, ensure_ascii=False))
            except AIError as exc:
                log.warning("[AI] %s", exc)
                if exc.fatal:
                    break
                continue
            got = 0
            for obj in items:
                idx = obj.get("i")
                if not isinstance(idx, int) or not 0 <= idx < len(batch):
                    continue
                info = _to_info(obj)
                if not (info.en or info.ru or info.query):
                    continue
                k = batch[idx]
                result[k] = info
                cache[k] = {"info": info.__dict__, "model": model, "ts": int(time.time())}
                got += 1
            if model in models and models[0] != model:
                models = [model, *(candidate for candidate in models if candidate != model)]
            save_ai_cache(cache)  # после каждой пачки: прогресс не пропадёт при сбое
            log.info("    получено и сохранено: %d из %d", got, len(batch))
    except Exception as exc:  # noqa: BLE001 — ИИ не должен ронять сборку дашборда
        log.warning("[AI] ошибка, продолжаю без ИИ: %s", exc)
    return result


def run_nexus_ai_analysis(
    rows: list[ModRow],
    settings: "Settings",
    args: argparse.Namespace,
) -> dict[int, NexusModRecord]:
    mod_ids = {r.mod_id for r in rows if is_valid_mod_id(r.mod_id)}
    cache_path = SCRIPT_DIR / "nexus_analysis_cache.json"
    cfg = settings.cfg or {}
    ai_cfg = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    key = key or str(ai_cfg.get("api_key") or "").strip()
    if not key or key.upper().startswith(("YOUR_", "YOUR-")):
        log.warning("[Nexus AI] Нет ключа OpenRouter; использую только сохранённый анализ.")
        return fetch_nexus_mod_records(mod_ids, cache_path, 0)

    records = fetch_nexus_mod_records(
        mod_ids,
        cache_path,
        args.nexus_max_requests,
    )
    pending = [
        record for _, record in sorted(records.items())
        if record.description and record.recommendation is None
    ]
    max_mods = args.nexus_ai_max_mods
    if len(pending) > max_mods:
        log.info(
            "[Nexus AI] За лимитом анализа оставлено %d модов; максимум за запуск: %d",
            len(pending) - max_mods,
            max_mods,
        )
    pending = pending[:max_mods]
    if not pending:
        log.info("[Nexus AI] Все доступные страницы уже проанализированы или недоступны.")
        return records

    configured_models = ai_cfg.get("models")
    if not isinstance(configured_models, list) or not configured_models:
        configured_models = DEFAULT_AI_MODELS
    models_value = [args.ai_model] if args.ai_model else configured_models
    models = [str(model) for model in models_value if str(model).strip()]
    if not models:
        log.error("[Nexus AI] Список моделей OpenRouter пуст.")
        return records

    batches = [
        pending[index:index + ANALYSIS_BATCH_SIZE]
        for index in range(0, len(pending), ANALYSIS_BATCH_SIZE)
    ]
    log.info(
        "[Nexus AI] Анализирую %d страниц модов; до %d запросов к OpenRouter, модель: %s",
        len(pending),
        len(batches),
        models[0],
    )
    log.info(
        "[Nexus AI] ИИ получает только публичные данные страниц Nexus; "
        "имена архивов и локальные пути не отправляются."
    )
    for batch_index, batch in enumerate(batches, 1):
        try:
            model, items = _ai_ask(
                models,
                key,
                build_analysis_payload(batch),
                system_prompt=ANALYSIS_SYSTEM_PROMPT,
                max_tokens=3000,
            )
        except AIError as exc:
            log.warning("[Nexus AI] %s", exc)
            if exc.fatal:
                break
            continue
        recommendations = parse_recommendations(items, batch, model)
        if not recommendations:
            log.warning("[Nexus AI] Модель %s не вернула проверяемых результатов для пачки %d.",
                        model, batch_index)
            continue
        for record in batch:
            recommendation = recommendations.get(record.mod_id)
            if recommendation is not None:
                records[record.mod_id] = replace(record, recommendation=recommendation)
        log.info(
            "[Nexus AI] Пачка %d/%d: сохранено рекомендаций %d/%d.",
            batch_index,
            len(batches),
            len(recommendations),
            len(batch),
        )
        # Persist every successful batch so a later network failure doesn't lose progress.
        save_nexus_mod_records(records, cache_path)

    return records


def cmd_ai_models(args: argparse.Namespace) -> int:
    """Список бесплатных моделей OpenRouter (ключ не нужен)."""
    try:
        data = _http_json(f"{OPENROUTER_BASE}/models")
    except AIError as exc:
        log.error("Не удалось получить список: %s", exc)
        return 1
    free = [m for m in data.get("data", []) if str(m.get("id", "")).endswith(":free")]
    free.sort(key=lambda m: (not str(m["id"]).startswith("nvidia/"), m["id"]))
    log.info("Бесплатные модели OpenRouter сейчас (сначала NVIDIA):")
    for m in free:
        log.info("  %-58s контекст %s", m["id"], m.get("context_length", "?"))
    log.info("")
    if not free:
        log.warning("Сейчас API не вернул моделей с суффиксом :free.")
        return 0
    log.info("Скопируйте этот JSON-фрагмент внутрь объекта \"ai\" в config.json:")
    print(json.dumps({"models": [m["id"] for m in free]}, ensure_ascii=False, indent=2), flush=True)
    log.info("")
    log.info("Модели из списка будут пробоваться по порядку. При ошибке/лимите переключение "
             "на следующую модель происходит без ожидания; успешная модель станет первой для следующих пачек.")
    log.info("Лимиты всей учётной записи, например суточная квота, переключением модели не обходятся.")
    return 0


# ==================== HTML ====================

BADGES = {
    "danger": ("badge-danger", "⚠️ ОПАСНО"),
    "dup": ("badge-dup", "⚡ ТОЧНАЯ КОПИЯ"),
    "old": ("badge-old", "🕓 СТАРАЯ ВЕРСИЯ"),
    "update": ("badge-update", "⬆ ОБНОВИТЬ В VORTEX"),
    "both": ("badge-both", "ОБА (РАЗНЫЕ ФАЙЛЫ)"),
    "vortex": ("badge-vortex", "VORTEX"),
    "local": ("badge-local", "DOWNLOADS"),
}
STATUS_ORDER = {"danger": 0, "dup": 1, "old": 2, "update": 3, "both": 4, "vortex": 5, "local": 6}
CHIPS = {
    EXACT_DUP: ("chip-dup", "точная копия"),
    OLDER: ("chip-old", "старая версия"),
    NEWER: ("chip-new", "новее, чем в Vortex"),
    STALE: ("chip-stale", "есть новее в Downloads"),
}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_file_item(a: Archive, key: int) -> str:
    parts = ['<div class="file-item" data-fid="%d">' % key]
    if a.is_candidate:
        checked = " checked" if a.verdict == EXACT_DUP else ""
        parts.append(f'<input type="checkbox" class="sel" data-fid="{key}"{checked} '
                     f'title="Отметить для очистки">')
    parts.append(f'<span class="file-name" title="{_esc(a.path)}">{_esc(a.name)}</span>')
    if a.verdict in CHIPS:
        cls, text = CHIPS[a.verdict]
        parts.append(f'<span class="chip {cls}">{text}</span>')
    parts.append(f'<span class="file-size">{fmt_size(a.size)}</span>')
    parts.append(f'<button type="button" class="btn-copy" data-copy-path="{_esc(a.path)}" '
                 f'title="Копировать полный путь">📋 Путь</button>')
    if a.note:
        parts.append(f'<div class="file-note">{_esc(a.note)}</div>')
    parts.append("</div>")
    return "".join(parts)


def nexus_search_url(title: str) -> str:
    return f"https://www.nexusmods.com/{GAME_NAME}/search/?gsearch={quote(title)}"


def google_search_url(title: str) -> str:
    """Точный поиск по страницам модов: site:nexusmods.com/<игра>/mods "<название>"."""
    q = f'site:nexusmods.com/{GAME_NAME}/mods "{title}"'
    return "https://www.google.com/search?q=" + quote_plus(q, safe=":/")


def clean_english_title(info: Optional[AIInfo], file_title: str) -> str:
    """Название для поиска: clean_title ИИ → search_query ИИ → название из файла, если оно чисто латинское."""
    for cand in (info.en if info else "", info.query if info else "", file_title):
        title = clean_search_title(cand)
        if title:
            return title
    return ""


def known_ids_from_ai(rows: list[ModRow], ai_map: dict[str, AIInfo]) -> dict[str, int]:
    """{norm_title: Mod ID} для модов без ID, чьё английское название от ИИ есть в KNOWN_MOD_IDS."""
    found: dict[str, int] = {}
    for r in rows:
        info = ai_map.get(norm_title(r.title))
        if r.mod_id is not None or info is None or info.conf == "low":
            continue
        for cand in (info.en, info.query):
            mid = known_mod_id(cand)
            if mid is not None:
                found[norm_title(r.title)] = mid
                break
    return found


def render_ai_note(info: Optional[AIInfo]) -> str:
    if info is None or not (info.en or info.ru or info.kind):
        return ""
    mark = {"high": "", "medium": "≈ "}.get(info.conf, "? ")
    ru = info.ru if info.ru.lower() != info.en.lower() else ""
    text = " · ".join(p for p in (info.en, ru, info.kind) if p)
    return ('<div class="ai-note" style="font-size:.82em;opacity:.8;margin-top:3px;" '
            'title="Догадка ИИ по имени файла (≈ — средняя уверенность, ? — низкая). Проверьте по ссылке.">'
            f'🤖 {mark}{_esc(text)}</div>')


def render_installation_note(note: Optional[InstallationNote]) -> str:
    if note is None:
        return ""
    if note.method == "unavailable":
        excerpt = f"Nexus API не предоставил описание (HTTP {note.error_code})."
    else:
        excerpt = _esc(note.excerpt) if note.excerpt else "Явные указания по установке в описании не найдены."
    return (
        '<details class="author-install-note" style="font-size:.82em;opacity:.9;margin-top:5px;">'
        f'<summary>📄 По описанию автора: {_esc(method_label(note.method))}</summary>'
        f'<div style="margin-top:4px;">{excerpt}<br>'
        f'<a href="https://www.nexusmods.com/{GAME_NAME}/mods/{note.mod_id}" target="_blank" '
        f'rel="noopener">Источник на Nexus ↗</a> · проверено {format_checked_date(note.checked_at)}</div>'
        '</details>'
    )


def render_mod_analysis(record: Optional[NexusModRecord]) -> str:
    if record is None:
        return ""
    if record.error_code is not None:
        return (
            '<div class="mod-analysis" style="font-size:.82em;opacity:.85;margin-top:5px;">'
            f'Nexus не предоставил страницу (HTTP {record.error_code}).</div>'
        )
    recommendation = record.recommendation
    if recommendation is None:
        return ""
    author_excerpt = extract_installation_note(record.mod_id, record.description).excerpt
    evidence = (
        f'<div style="margin-top:5px;"><strong>Фрагмент инструкции автора:</strong> '
        f'{_esc(author_excerpt)}</div>'
        if author_excerpt else
        '<div style="margin-top:5px;">Явную инструкцию по установке в описании не нашли.</div>'
    )
    return (
        '<details class="mod-analysis" style="font-size:.82em;opacity:.92;margin-top:6px;">'
        f'<summary>🤖 ИИ: {_esc(analysis_label(recommendation.method))} '
        f'({recommendation.confidence}% уверенности модели)</summary>'
        f'<div style="margin-top:5px;"><strong>Что меняет:</strong> '
        f'{_esc(recommendation.summary_ru)}<br>'
        f'<strong>Тип:</strong> {_esc(analysis_label(recommendation.category))}; '
        f'<strong>охват:</strong> {_esc(analysis_label(recommendation.impact))}.<br>'
        f'<strong>Почему:</strong> {_esc(recommendation.rationale_ru)}'
        f'{evidence}<br>'
        f'<a href="https://www.nexusmods.com/{GAME_NAME}/mods/{record.mod_id}" target="_blank" '
        f'rel="noopener">Страница Nexus (источник) ↗</a>'
        '</div></details>'
    )


def render_row(row: ModRow, index_of: dict[int, int],
               ai_map: Optional[dict[str, AIInfo]] = None,
               custom_blacklist: Optional[dict[int, str]] = None,
               installation_notes: Optional[dict[int, InstallationNote]] = None,
               nexus_records: Optional[dict[int, NexusModRecord]] = None) -> str:
    info = (ai_map or {}).get(norm_title(row.title))
    badge_cls, badge_text = BADGES[row.status]
    if is_valid_mod_id(row.mod_id):
        link = (f'<a href="https://www.nexusmods.com/{GAME_NAME}/mods/{row.mod_id}" target="_blank" '
                f'rel="noopener" class="nexus-link">#{row.mod_id} ↗</a>')
    else:
        title = clean_english_title(info, row.title)
        ai_id = ""
        if info and info.mod_id is not None and is_valid_mod_id(info.mod_id):
            ai_id = (f'<a href="https://www.nexusmods.com/{GAME_NAME}/mods/{info.mod_id}" target="_blank" '
                     f'rel="noopener" class="search-link" '
                     f'title="ID предположил ИИ — не подтверждён, проверьте страницу">≈ #{info.mod_id} ИИ ↗</a> ')
        if title:
            link = (ai_id +
                    f'<a href="{_esc(nexus_search_url(title))}" target="_blank" rel="noopener" '
                    f'class="search-link" title="Поиск на Nexus: {_esc(title)}">Nexus ↗</a> '
                    f'<a href="{_esc(google_search_url(title))}" target="_blank" rel="noopener" '
                    f'class="search-link" title="Google по страницам модов: «{_esc(title)}»">Google ↗</a>')
        else:   # нет английского названия (имя файла русское, ИИ не запускался) — искать нечем
            link = ai_id + ('<span class="empty-val" title="Английское название неизвестно: '
                            'запустите с --ai">нет названия (--ai)</span>')

    def cell(source: str) -> tuple[str, int]:
        items = [f for f in row.files if f.source == source]
        if not items:
            return '<span class="empty-val">—</span>', 0
        return "".join(render_file_item(f, index_of[f.id]) for f in items), sum(f.size for f in items)

    dl_html, dl_size = cell("downloads")
    vx_html, vx_size = cell("vortex")

    action_lines = [_esc(a) for a in row.actions]
    if row.danger:
        action_lines.insert(0, f'<strong style="color:var(--accent-red);">{_esc(row.danger)}</strong>')
    elif row.status == "dup":
        action_lines[0] = f'<strong style="color:var(--accent-yellow);">{action_lines[0]}</strong>'
    action_html = "<br>".join(action_lines)
    if row.mod_id is not None:
        action_html += render_installation_note((installation_notes or {}).get(row.mod_id))
        action_html += render_mod_analysis((nexus_records or {}).get(row.mod_id))
    if is_valid_mod_id(row.mod_id):
        custom_entries = custom_blacklist or {}
        if row.mod_id in custom_entries:
            custom_reason = custom_entries[row.mod_id]
            blacklist_button = (
                f'<button type="button" class="btn-blacklist" data-mod-id="{row.mod_id}" '
                f'data-custom="true" title="Причина: {_esc(custom_reason)}">'
                'Убрать из моего списка</button>')
        elif row.mod_id in OUTDATED_OR_DANGEROUS:
            blacklist_button = (
                f'<button type="button" class="btn-blacklist" data-mod-id="{row.mod_id}" '
                'disabled title="Эта отметка задана встроенным списком и не удаляется из дашборда">'
                'Встроенный список</button>')
        else:
            blacklist_button = (
                f'<button type="button" class="btn-blacklist" data-mod-id="{row.mod_id}" '
                'data-custom="false">Добавить в мой список</button>')
        action_html += '<div class="blacklist-action">' + blacklist_button + '</div>'

    extra = [info.en, info.ru, info.query, info.kind] if info else []
    install_note = (installation_notes or {}).get(row.mod_id) if row.mod_id is not None else None
    if install_note:
        extra.append(install_note.excerpt)
    analysis_record = (nexus_records or {}).get(row.mod_id) if row.mod_id is not None else None
    if analysis_record and analysis_record.recommendation:
        extra.extend((
            analysis_record.recommendation.summary_ru,
            analysis_record.recommendation.rationale_ru,
        ))
    search = " ".join([row.title, str(row.mod_id or ""), row.danger, *extra] + [f.name for f in row.files]).lower()
    group_note = (f'<div class="group-note" title="Почему эти архивы объединены: '
                  f'{_esc(row.grouping_reason)}">Группа: {_esc(row.grouping_reason)}</div>')
    return (
        f'<tr class="row-{row.status}" data-cats="{" ".join(sorted(row.cats))}" data-search="{_esc(search)}">'
        f'<td class="col-status" data-v="{STATUS_ORDER[row.status]}"><span class="badge {badge_cls}">{badge_text}</span></td>'
        f'<td class="col-id" data-v="{row.mod_id or ""}"><code>{row.mod_id or "—"}</code></td>'
        f'<td class="col-title" data-v="{_esc(row.title.lower())}"><strong>{_esc(row.title)}</strong>{group_note}{render_ai_note(info)}</td>'
        f'<td class="col-link">{link}</td>'
        f'<td class="col-action" data-v="{_esc(row.actions[0])}">{action_html}</td>'
        f'<td class="col-files" data-v="{dl_size}">{dl_html}</td>'
        f'<td class="col-files" data-v="{vx_size}">{vx_html}</td>'
        f"</tr>"
    )


def render_game_mod_entry(entry: GameModEntry) -> str:
    relative = entry.path.as_posix()
    search = f"{entry.section} {entry.name} {relative}".lower()
    return (
        f'<tr data-search="{_esc(search)}">'
        f'<td><code>{_esc(entry.section)}</code></td>'
        f'<td><strong>{_esc(entry.name)}</strong>'
        f'<div class="group-note">{_esc(relative)}</div></td>'
        f'<td data-v="{entry.file_count}">{entry.file_count}</td>'
        f'<td data-v="{entry.size}">{fmt_size(entry.size)}</td>'
        f'<td><button type="button" class="btn-reveal" data-path="{_esc(entry.path)}" '
        f'title="Открыть в Проводнике">📂 В папке</button></td>'
        f'</tr>'
    )


def render_dashboard(core_status: dict[str, bool], rows: list[ModRow],
                     archives: list[Archive], ai_map: Optional[dict[str, AIInfo]] = None,
                     installation_notes: Optional[dict[int, InstallationNote]] = None,
                     nexus_records: Optional[dict[int, NexusModRecord]] = None,
                     game_mods: Optional[list[GameModEntry]] = None,
                     game_path: Optional[Path] = None) -> str:
    game_mods = game_mods or []
    n_exact = sum(1 for a in archives if a.verdict == EXACT_DUP)
    n_old = sum(1 for a in archives if a.verdict == OLDER)
    reclaim = sum(a.size for a in archives if a.verdict == EXACT_DUP)
    counts = {c: sum(1 for r in rows if c in r.cats)
              for c in ("all", "dup", "old", "update", "danger", "vortex", "nexus_downloads")}
    counts["local"] = len(game_mods)
    source_summary = summarize_sources(archives, rows)

    # Данные для JS (чекбоксы, экспорт). Индексы — по порядку отображения.
    ordered = [f for r in rows for f in r.files]
    index_of = {a.id: i for i, a in enumerate(ordered)}
    by_id = {a.id: a for a in archives}
    file_data = [{
        "s": a.source, "n": a.name, "p": str(a.path), "z": a.size, "m": a.mtime,
        "id": a.meta.mod_id, "t": a.meta.title, "v": a.meta.version,
        "vd": a.verdict, "nt": a.note, "h": a.sha256,
        "k": str(by_id[a.keeper].path) if a.keeper is not None else "",
    } for a in ordered]
    file_json = json.dumps(file_data, ensure_ascii=False).replace("</", "<\\/")

    stats = (
        f'Групп модов: <strong>{counts["all"]}</strong> &bull; '
        f'архивов: <strong>{len(archives)}</strong> &bull; '
        f'точных копий: <strong style="color:var(--accent-yellow);">{n_exact}</strong> '
        f'(<strong>{fmt_size(reclaim)}</strong>) &bull; '
        f'старых версий: <strong>{n_old}</strong> &bull; '
        f'опасных: <strong style="color:var(--accent-red);">{counts["danger"]}</strong><br>'
        f'Группы только в Downloads: <strong>{source_summary.downloads_only_groups}</strong>; '
        f'только в Vortex: <strong>{source_summary.vortex_only_groups}</strong>; '
        f'в обеих папках: <strong>{source_summary.both_source_groups}</strong>. '
        f'Архивов по папкам — Downloads: <strong>{source_summary.downloads_archives}</strong>, '
        f'Vortex: <strong>{source_summary.vortex_archives}</strong>.'
    )

    if core_status:
        cards = "".join(
            f'<div class="core-card {"core-ok" if ok else "core-missing"}">'
            f'<div class="core-icon">{"✔" if ok else "✘"}</div><div class="core-info">'
            f'<div class="core-name">{_esc(name)}</div>'
            f'<div class="core-val">{"УСТАНОВЛЕН" if ok else "ОТСУТСТВУЕТ"}</div></div></div>'
            for name, ok in core_status.items())
    else:
        cards = '<div class="core-card"><div class="core-info"><div class="core-val">Папка игры не задана (--game) — проверка фреймворков пропущена</div></div></div>'

    buttons = "".join(
        f'<button type="button" class="btn" data-filter="{key}"'
        f'{title_attr}>{label} ({counts[key]})</button>'
        for key, label, title_attr in (
            ("all", "Все группы", ""),
            ("dup", "⚡ Точные копии", ""),
            ("old", "🕓 Старые версии", ""),
            ("update", "⬆ Обновить в Vortex", ""),
            ("danger", "⚠️ Опасные", ""),
            ("vortex", "Моды в Vortex Downloads", ""),
            ("local", "Локальные в игре", ' title="Инвентарь известных папок модов внутри папки игры; Vortex-развёрнутые файлы тоже видны."'),
            ("nexus_downloads", "Из Nexus Downloads", ' title="Все группы с архивами из downloads, включая те, что также есть в Vortex."'),
        )
    )

    if game_path is None:
        game_status = "Папка игры не найдена. Укажите путь `game` в config.json."
    elif not game_path.is_dir():
        game_status = f"Папка игры не найдена: {game_path}"
    elif game_mods:
        game_status = (
            f"Найдено элементов: {len(game_mods)}. Сканируются только известные папки модов; "
            "это не гарантированное число отдельных модов. Файлы, развернутые Vortex в игру, тоже учитываются."
        )
    else:
        game_status = (
            "В известных папках модификаций файлы не найдены. Изменения в других местах игры "
            "этот инвентарь может не обнаружить."
        )

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    custom_blacklist = load_custom_blacklist()
    values = {
        "GENERATED": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "STATS": stats,
        "CORE_CARDS": cards,
        "FILTER_BUTTONS": buttons,
        "GAME_STATUS": _esc(game_status),
        "GAME_ROWS": "\n".join(render_game_mod_entry(entry) for entry in game_mods),
        "ROWS": "\n".join(
            render_row(r, index_of, ai_map, custom_blacklist, installation_notes, nexus_records)
            for r in rows
        ),
        "FILE_DATA": file_json,
    }
    return re.sub(r"@@([A-Z_]+)@@", lambda m: values[m.group(1)], template)


# ==================== НАСТРОЙКИ ====================

@dataclass
class Settings:
    downloads: Optional[Path]
    vortex: Optional[Path]
    game: Optional[Path]
    out: Path
    quarantine: Path
    cfg: Optional[dict] = None


def _as_path(value: object) -> Optional[Path]:
    if not value:
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def load_settings(args: argparse.Namespace) -> Settings:
    global MAX_MOD_ID
    cfg: dict = {}
    cfg_path = _as_path(getattr(args, "config", None)) or CONFIG_PATH
    if cfg_path.is_file():
        try:
            loaded_cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.error("Не удалось прочитать %s: %s", cfg_path, exc)
            sys.exit(2)
        if not isinstance(loaded_cfg, dict):
            log.error("В %s ожидается JSON-объект с настройками.", cfg_path)
            sys.exit(2)
        cfg = loaded_cfg

    custom_max = cfg.get("max_mod_id")
    if isinstance(custom_max, int) and not isinstance(custom_max, bool) and custom_max >= 1:
        MAX_MOD_ID = custom_max

    downloads = _as_path(getattr(args, "downloads", None) or cfg.get("downloads"))
    vortex = _as_path(getattr(args, "vortex", None) or cfg.get("vortex"))
    game = _as_path(getattr(args, "game", None) or cfg.get("game")) or find_cyberpunk_install()

    # Всегда сохраняем дашборд в папке скрипта (проекта), а не в Downloads
    out = _as_path(getattr(args, "out", None) or cfg.get("out")) or SCRIPT_DIR / "mod_audit_dashboard.html"
    quarantine = _as_path(getattr(args, "quarantine", None) or cfg.get("quarantine")) \
        or (downloads or SCRIPT_DIR) / QUARANTINE_DIRNAME
    return Settings(downloads, vortex, game, out, quarantine, cfg)


# ==================== КОМАНДА scan ====================

def cmd_scan(args: argparse.Namespace) -> int:
    s = load_settings(args)
    if not (s.downloads or s.vortex):
        log.error("Не заданы папки. Укажите --downloads/--vortex или заполните config.json "
                  "(пример — config.example.json).")
        return 2

    archives: list[Archive] = []
    scanned_folders = 0
    scan_labels = {"downloads": "Downloads", "vortex": "Vortex"}
    for root in build_scan_plan(s.downloads, s.vortex):
        label = scan_labels[root.source]
        folder = root.folder
        if root.warning:
            log.warning("[!] %s", root.warning)
        if not folder.is_dir():
            log.warning("[!] Папка %s не найдена: %s", label, folder)
            continue
        scanned_folders += 1
        log.info("[*] Сканирование %s: %s", label, folder)
        found = scan_folder(folder, root.source, start_id=len(archives), exclude=root.exclude)
        archives += found
        log.info("[*] Архивов найдено в %s: %d", label, len(found))
    if not scanned_folders:
        log.error("Не найдена ни одна папка для сканирования. Проверьте downloads/vortex в config.json.")
        return 2

    rejected = [a for a in archives if a.meta.rejected_id is not None]
    if rejected:
        log.info("[!] Отброшено ложных Mod ID (вне 1–%d): %d архивов — ссылки на них только через поиск/ИИ",
                 MAX_MOD_ID, len(rejected))
        for a in rejected[:10]:
            log.info("    %s  (число %d)", a.name, a.meta.rejected_id)

    mark_exact_duplicates(archives)
    mark_versions(archives)
    blacklist = load_blacklist()
    rows = build_rows(archives, blacklist)   # известные моды опознаются по имени файла
    core = check_game_core(s.game)
    game_mods = scan_game_mods(s.game)
    log.info("[*] Найдено элементов в известных папках модов игры: %d", len(game_mods))
    # Без AI-флагов подтягивается только кэш (ai_cache.json), сеть не используется.
    ai_map = run_ai(rows, s, args, online=bool(args.ai or args.ai_all or args.ai_auto))
    # Английские названия от ИИ тоже сверяются со словарём; при совпадении строки пересобираются.
    ai_ids = known_ids_from_ai(rows, ai_map)
    if ai_ids:
        rows = build_rows(archives, blacklist, ai_ids)
    n_known = sum(1 for r in rows if r.mod_id is not None and all(f.meta.mod_id is None for f in r.files))
    if n_known:
        log.info("[ID] Опознано по встроенному словарю KNOWN_MOD_IDS: %d (прямая ссылка вместо поиска)", n_known)

    installation_notes: dict[int, InstallationNote] = {}
    nexus_records: dict[int, NexusModRecord] = {}
    if args.nexus_analyze:
        nexus_records = run_nexus_ai_analysis(rows, s, args)
    elif args.nexus_details:
        installation_notes = load_installation_notes(
            {r.mod_id for r in rows if is_valid_mod_id(r.mod_id)},
            SCRIPT_DIR / "nexus_install_cache.json",
            args.nexus_max_requests,
        )

    s.out.parent.mkdir(parents=True, exist_ok=True)
    s.out.write_text(
        render_dashboard(
            core, rows, archives, ai_map, installation_notes, nexus_records,
            game_mods, s.game,
        ),
        encoding="utf-8",
    )

    n_exact = [a for a in archives if a.verdict == EXACT_DUP]
    log.info("")
    log.info("=" * 60)
    log.info("[+] Архивов: %d, групп: %d, точных копий: %d (%s)",
             len(archives), len(rows), len(n_exact), fmt_size(sum(a.size for a in n_exact)))
    log.info("[+] Страница: %s", s.out)
    log.info("=" * 60)
    if not args.no_open:
        webbrowser.open(s.out.resolve().as_uri())
    return 0


# ==================== КОМАНДА clean ====================

def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def verify_entry(
    entry: object,
    downloads: Path,
    quarantine: Path,
    verify_hash: bool,
) -> Optional[str]:
    """Возвращает текст ошибки, если файл трогать нельзя, иначе None."""
    if not isinstance(entry, dict):
        return "неверный формат записи"
    path_value = entry.get("path")
    keeper_value = entry.get("keeper")
    size = entry.get("size")
    raw_mtime = entry.get("mtime")
    reason = entry.get("reason")
    if (
        not isinstance(path_value, str) or not path_value or "\0" in path_value
        or not isinstance(keeper_value, str) or not keeper_value or "\0" in keeper_value
        or not isinstance(size, int) or isinstance(size, bool) or size < 0
        or not isinstance(reason, str) or reason not in {EXACT_DUP, OLDER}
        or isinstance(raw_mtime, bool)
    ):
        return "неверные поля записи"
    try:
        mtime = float(raw_mtime)
    except (TypeError, ValueError, OverflowError):
        return "неверная дата изменения в записи"
    if not math.isfinite(mtime):
        return "неверная дата изменения в записи"
    sha256 = entry.get("sha256")
    if sha256 is not None and not isinstance(sha256, str):
        return "неверный SHA-256 в записи"

    path = Path(path_value)
    keeper = Path(keeper_value)
    if path.suffix.lower() not in EXTENSIONS:
        return "не архив мода"
    if not _inside(path, downloads):
        return "вне папки Downloads"
    if _inside(path, quarantine):
        return "уже в карантине"
    if not path.is_file():
        return "файл не найден"
    try:
        st = path.stat()
    except OSError:
        return "не удалось прочитать файл"
    if st.st_size != size or abs(st.st_mtime - mtime) > 2:
        return "файл изменился после сканирования"
    if not keeper.is_file():
        return "не найден файл, который должен остаться"
    if keeper.resolve() == path.resolve():
        return "файл совпадает с «остающимся»"
    if entry.get("reason") == EXACT_DUP and verify_hash:
        try:
            keeper_size = keeper.stat().st_size
        except OSError:
            return "не удалось прочитать остающийся файл"
        if keeper_size != st.st_size:
            return "размер остающегося файла не совпадает"
        try:
            h = sha256_file(path)
            keeper_hash = sha256_file(keeper)
        except OSError:
            return "не удалось проверить SHA-256"
        if h != keeper_hash or (sha256 and h != sha256):
            return "SHA-256 не совпадает с остающимся файлом"
    return None


def cmd_clean(args: argparse.Namespace) -> int:
    s = load_settings(args)
    if s.downloads is None:
        log.error("Для очистки нужна папка Downloads (--downloads или config.json): "
                  "файлы вне её трогать нельзя.")
        return 2
    try:
        data = json.loads(Path(args.selection).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise ValueError("ожидается объект JSON с массивом files")
        entries = data["files"]
        if any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("каждый элемент files должен быть объектом")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.error("Не удалось прочитать список очистки: %s", exc)
        return 2

    plan: list[dict] = []
    for e in entries:
        problem = verify_entry(e, s.downloads, s.quarantine, verify_hash=not args.no_verify)
        if problem:
            log.warning("[ПРОПУСК] %s — %s", e.get("path"), problem)
        else:
            plan.append(e)
            log.info("[ОК] %s  (%s) → остаётся %s", e["path"], fmt_size(e["size"]), e["keeper"])

    total = sum(e["size"] for e in plan)
    log.info("")
    log.info("К очистке: %d из %d файлов, освободится %s", len(plan), len(entries), fmt_size(total))
    if not args.apply:
        log.info("DRY-RUN: ничего не изменено. Добавьте --apply, чтобы выполнить.")
        return 0
    if not plan:
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = s.quarantine / stamp
    trash = None
    if args.mode == "trash":
        try:
            from send2trash import send2trash as trash  # type: ignore[import-not-found]
        except ImportError:
            log.error("Для --mode trash нужен пакет send2trash: pip install send2trash "
                      "(или используйте --mode quarantine).")
            return 2
    else:
        run_dir.mkdir(parents=True, exist_ok=True)

    log_path = (run_dir if args.mode != "trash" else s.quarantine) / f"clean_{stamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    moved = 0
    with open(log_path, "w", encoding="utf-8") as lf:
        for e in plan:
            src = Path(e["path"])
            try:
                if trash:
                    trash(str(src))
                    lf.write(f"TRASH\t{src}\n")
                else:
                    dest = run_dir / src.name
                    n = 1
                    while dest.exists():
                        dest = run_dir / f"{src.stem}__{n}{src.suffix}"
                        n += 1
                    shutil.move(str(src), str(dest))
                    lf.write(f"MOVE\t{src}\t{dest}\n")
                moved += 1
            except OSError as exc:
                log.error("Не удалось обработать %s: %s", src, exc)
                lf.write(f"FAIL\t{src}\t{exc}\n")
    where = "в Корзину" if trash else f"в {run_dir}"
    log.info("Готово: обработано %d из %d файлов (%s). Журнал: %s", moved, len(plan), where, log_path)
    return 0


# ==================== CLI ====================

def build_parser() -> argparse.ArgumentParser:
    def nonnegative_int(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("ожидается целое число") from exc
        if parsed < 0:
            raise argparse.ArgumentTypeError("число не может быть отрицательным")
        return parsed

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="путь к config.json (по умолчанию — рядом со скриптом)")
    common.add_argument("--downloads", help="папка с загрузками Nexus (Downloads)")
    common.add_argument("--vortex", help="папка загрузок Vortex")
    common.add_argument("--game", help="папка игры (по умолчанию ищется в Steam)")
    common.add_argument("--quarantine", help=f"папка карантина (по умолчанию <downloads>\\{QUARANTINE_DIRNAME})")

    parser = argparse.ArgumentParser(description="Cyberpunk 2077 Mod Auditor")
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", parents=[common], help="собрать дашборд (по умолчанию)")
    p_scan.add_argument("--out", help="куда сохранить HTML")
    p_scan.add_argument("--no-open", action="store_true", help="не открывать отчёт в браузере")
    p_scan.add_argument("--ai", action="store_true",
                        help="ИИ (OpenRouter): английские названия и запросы для поиска на Nexus (моды без ID)")
    p_scan.add_argument("--ai-auto", action="store_true",
                        help="использовать ИИ, если задан ключ OpenRouter; иначе сканировать без сети")
    p_scan.add_argument("--ai-all", action="store_true", help="ИИ-анализ для всех модов, не только без ID")
    p_scan.add_argument("--ai-refresh", action="store_true", help="игнорировать кэш ИИ и спросить заново")
    p_scan.add_argument("--ai-model", help="id модели OpenRouter (по умолчанию — бесплатные NVIDIA Nemotron)")
    p_scan.add_argument("--ai-max-requests", type=nonnegative_int, default=None,
                        help=f"максимум запросов за запуск (по умолчанию {AI_DEFAULT_MAX_REQUESTS})")
    p_scan.add_argument("--nexus-details", action="store_true",
                        help="получить с Nexus подсказки автора по установке (опционально, без ключа API)")
    p_scan.add_argument("--nexus-max-requests", type=nonnegative_int, default=20,
                        help="максимум запросов к Nexus за запуск (по умолчанию 20)")
    p_scan.add_argument("--nexus-analyze", action="store_true",
                        help="ИИ анализирует страницы Nexus, переводит описание и рекомендует способ установки")
    p_scan.add_argument("--nexus-ai-max-mods", type=nonnegative_int, default=10,
                        help="максимум страниц для AI-анализа за запуск (по умолчанию 10)")
    p_scan.set_defaults(func=cmd_scan)

    p_models = sub.add_parser("ai-models", help="показать бесплатные модели OpenRouter")
    p_models.set_defaults(func=cmd_ai_models)

    p_clean = sub.add_parser("clean", parents=[common], help="применить список очистки из дашборда")
    p_clean.add_argument("selection", help="JSON, скачанный из дашборда")
    p_clean.add_argument("--apply", action="store_true", help="выполнить (без флага — только проверка)")
    p_clean.add_argument("--mode", choices=("quarantine", "trash"), default="quarantine",
                         help="quarantine — перенос в папку карантина; trash — в Корзину (нужен send2trash)")
    p_clean.add_argument("--no-verify", action="store_true",
                         help="не пересчитывать SHA-256 перед переносом точных копий")
    p_clean.set_defaults(func=cmd_clean)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("scan", "clean", "ai-models", "-h", "--help"):
        argv.insert(0, "scan")   # `python nexus_links.py --downloads X` = scan
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())