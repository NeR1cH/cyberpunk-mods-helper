"""Conservative persistent rate and daily request limits for OpenRouter calls."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Generator

log = logging.getLogger("modaudit")

DAILY_REQUEST_BUDGET = 10
PER_RUN_REQUEST_BUDGET = 2
MIN_REQUEST_INTERVAL_SECONDS = 3.5
USAGE_PATH = Path(__file__).resolve().parent / "openrouter_usage.json"
ANALYSIS_CACHE_PATH = Path(__file__).resolve().parent / "nexus_analysis_cache.json"
_last_request_at = 0.0
_requests_this_process = 0
_reservation_lock = threading.Lock()


@contextmanager
def _exclusive_file_lock(path: Path) -> Generator[None, None, None]:
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def reserve_request(
    path: Path = USAGE_PATH,
    daily_budget: int = DAILY_REQUEST_BUDGET,
    per_run_budget: int = PER_RUN_REQUEST_BUDGET,
) -> bool:
    """Persist one daily request slot before sending; fail closed on unreadable state."""
    global _requests_this_process
    if daily_budget < 0 or per_run_budget < 0:
        raise ValueError("request budgets must not be negative")

    with _reservation_lock:
        if _requests_this_process >= per_run_budget:
            log.warning(
                "[AI] Достигнут лимит OpenRouter за запуск: %d попытки. "
                "Остальной анализ продолжится при следующем запуске.",
                per_run_budget,
            )
            return False

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _exclusive_file_lock(path):
                return _reserve_daily_slot(path, daily_budget)
        except OSError as exc:
            log.error("[AI] Не удалось заблокировать счётчик OpenRouter (%s): %s", path, exc)
            return False


def block_remaining_requests_this_run() -> None:
    global _requests_this_process
    with _reservation_lock:
        _requests_this_process = max(_requests_this_process, PER_RUN_REQUEST_BUDGET)


def remaining_daily_requests(path: Path = USAGE_PATH) -> int | None:
    """Return today's remaining budget, or None when the ledger cannot be trusted."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(path):
            if not path.exists():
                return DAILY_REQUEST_BUDGET
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("[AI] Не удалось проверить дневной лимит OpenRouter: %s", exc)
        return None

    if not isinstance(data, dict):
        log.error("[AI] Счётчик лимита OpenRouter имеет неверный формат: %s", path)
        return None
    if data.get("date") != datetime.now(timezone.utc).date().isoformat():
        return DAILY_REQUEST_BUDGET

    used = data.get("requests")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        log.error("[AI] Счётчик запросов OpenRouter повреждён: %s", path)
        return None
    return max(0, DAILY_REQUEST_BUDGET - used)


def cached_analysis_page_count(path: Path = ANALYSIS_CACHE_PATH) -> int | None:
    """Return the number of Nexus pages cached, or None for an unreadable cache."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        log.error("[Nexus AI] Не удалось проверить кэш страниц Nexus: %s", exc)
        return None

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        log.error("[Nexus AI] Кэш страниц Nexus имеет неверный формат: %s", path)
        return None
    return len(items)


def _reserve_daily_slot(path: Path, daily_budget: int) -> bool:
    global _requests_this_process
    today = datetime.now(timezone.utc).date().isoformat()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("[AI] Не удалось прочитать счётчик лимита OpenRouter (%s): %s", path, exc)
            return False
        if not isinstance(data, dict):
            log.error("[AI] Счётчик лимита OpenRouter имеет неверный формат: %s", path)
            return False
        if data.get("date") == today:
            used = data.get("requests")
            if not isinstance(used, int) or isinstance(used, bool) or used < 0:
                log.error("[AI] Счётчик запросов OpenRouter повреждён: %s", path)
                return False
        else:
            used = 0
    else:
        used = 0

    if used >= daily_budget:
        log.warning(
            "[AI] Достигнут безопасный лимит OpenRouter: %d попыток за UTC-сутки. "
            "Сканирование продолжится без новых запросов.",
            daily_budget,
        )
        return False

    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps({"date": today, "requests": used + 1}, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except OSError as exc:
        log.error("[AI] Не удалось обновить счётчик лимита OpenRouter (%s): %s", path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            log.warning("[AI] Не удалось удалить временный файл счётчика: %s", temp_path)
        return False
    _requests_this_process += 1
    return True


def wait_for_request_slot() -> None:
    """Keep calls from this process below the common free-tier per-minute rate."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    wait = MIN_REQUEST_INTERVAL_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["progress"]:
        remaining = remaining_daily_requests()
        page_count = cached_analysis_page_count()
        if remaining is None or page_count is None:
            print("-1|-1")
            raise SystemExit(1)
        print(f"{remaining}|{page_count}")
        raise SystemExit(0)
    raise SystemExit("Использование: python openrouter_limits.py progress")
