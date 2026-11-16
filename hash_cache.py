"""Кэш хешей архивов: повторный скан не перечитывает гигабайты неизменившихся файлов.

Запись действительна, пока у файла те же размер и время изменения. Файл кэша
содержит локальные пути и в Git не добавляется (`mod_cache.json` в .gitignore).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from json_files import atomic_write_json

log = logging.getLogger("modaudit")

HASH_CACHE_VERSION = 1


def _is_hex(value: object, length: int) -> bool:
    return (isinstance(value, str) and len(value) == length
            and all(char in "0123456789abcdef" for char in value))


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    size, mtime_ns = entry.get("size"), entry.get("mtime_ns")
    return (
        isinstance(size, int) and not isinstance(size, bool) and size >= 0
        and isinstance(mtime_ns, int) and not isinstance(mtime_ns, bool)
        and _is_hex(entry.get("sha256"), 64)
        and _is_hex(entry.get("md5"), 32)
    )


class HashCache:
    """{normcase(путь): {size, mtime_ns, sha256, md5}} с отложенной атомарной записью."""

    def __init__(self, path: Path, items: dict[str, dict[str, Any]] | None = None) -> None:
        self.path = path
        self.items: dict[str, dict[str, Any]] = items or {}
        self.dirty = False

    @classmethod
    def load(cls, path: Path) -> "HashCache":
        """Нечитаемый или чужой файл — не ошибка: хеши просто посчитаются заново."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as exc:
            log.warning("[*] Кэш хешей не прочитан (%s): %s. Хеши будут посчитаны заново.", path, exc)
            return cls(path)
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or raw.get("version") != HASH_CACHE_VERSION \
                or not isinstance(items, dict):
            log.warning("[*] Кэш хешей имеет неизвестный формат (%s). Хеши будут посчитаны заново.", path)
            return cls(path)
        return cls(path, {key: entry for key, entry in items.items() if _valid_entry(entry)})

    @staticmethod
    def key(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.abspath(path))

    def get(self, path: str | os.PathLike[str], size: int, mtime_ns: int) -> dict[str, Any] | None:
        entry = self.items.get(self.key(path))
        if entry is not None and entry["size"] == size and entry["mtime_ns"] == mtime_ns:
            return entry
        return None

    def put(self, path: str | os.PathLike[str], size: int, mtime_ns: int,
            sha256: str, md5: str) -> None:
        self.items[self.key(path)] = {
            "size": size, "mtime_ns": mtime_ns, "sha256": sha256, "md5": md5,
        }
        self.dirty = True

    def prune(self, scanned_roots: Iterable[str | os.PathLike[str]],
              seen: Iterable[str | os.PathLike[str]]) -> int:
        """Убирает записи о файлах, которых больше нет в просканированных папках.

        Записи из папок, которые в этот раз не сканировались (например, диск
        отключён), остаются: иначе после подключения всё пришлось бы считать заново.
        """
        prefixes = [self.key(root).rstrip("\\/") + os.sep for root in scanned_roots]
        present = {self.key(path) for path in seen}
        stale = [key for key in self.items
                 if key not in present and any(key.startswith(prefix) for prefix in prefixes)]
        for key in stale:
            del self.items[key]
        if stale:
            self.dirty = True
        return len(stale)

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            atomic_write_json(self.path, {"version": HASH_CACHE_VERSION, "items": self.items},
                              indent=1)
        except OSError as exc:
            log.warning("[*] Кэш хешей не сохранён (%s): %s", self.path, exc)
        else:
            self.dirty = False
