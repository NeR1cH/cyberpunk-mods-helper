"""Атомарная запись JSON-файлов и сохранение повреждённых файлов под другим именем."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile

log = logging.getLogger("modaudit")


def atomic_write_json(path: Path, data: object, *, indent: int | None = 2) -> None:
    """Записать JSON так, чтобы по пути `path` всегда лежала целая версия файла.

    Данные пишутся во временный файл в той же папке и подменяют настоящий файл
    одним `os.replace`. При ошибке временный файл удаляется, а `OSError`
    передаётся вызывающему коду: только он знает, как сообщить о сбое.
    """
    text = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.remove(temp_name)
        except OSError as cleanup_exc:
            log.warning("Не удалось удалить временный файл %s: %s", temp_name, cleanup_exc)
        raise


def preserve_corrupt_json(path: Path) -> Path | None:
    """Переименовать нечитаемый файл в `<имя>.corrupt-<дата>.json`.

    Иначе следующее сохранение запишет поверх него пустые данные. Возвращает
    новый путь или None, если переименовать не удалось.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
    attempt = 1
    while target.exists():
        attempt += 1
        target = path.with_name(f"{path.stem}.corrupt-{stamp}-{attempt}{path.suffix}")
    try:
        path.rename(target)
    except OSError as exc:
        log.warning("Не удалось сохранить повреждённый файл %s как %s: %s", path, target.name, exc)
        return None
    log.warning("Повреждённый файл сохранён как %s; новые данные будут записаны в %s",
                target, path.name)
    return target
