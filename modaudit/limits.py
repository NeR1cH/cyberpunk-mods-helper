"""Conservative persistent rate and daily request limits for OpenRouter calls."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Generator

from modaudit import PROJECT_DIR
from modaudit.json_files import atomic_write_json

log = logging.getLogger("modaudit")

# Значения по умолчанию; меняются настройками ai.daily_budget и ai.per_run_budget в config.json.
# Один запуск теперь проходит все раунды ИИ сам (scan --ai-until-budget), поэтому лимит
# «за запуск», если он не задан, равен суточному.
DAILY_REQUEST_BUDGET = 10
PER_RUN_REQUEST_BUDGET = DAILY_REQUEST_BUDGET
MIN_REQUEST_INTERVAL_SECONDS = 3.5
USAGE_PATH = PROJECT_DIR / "openrouter_usage.json"
ANALYSIS_CACHE_PATH = PROJECT_DIR / "nexus_analysis_cache.json"
CONFIG_PATH = PROJECT_DIR / "config.json"
_daily_budget = DAILY_REQUEST_BUDGET
_per_run_budget = PER_RUN_REQUEST_BUDGET
_last_request_at = 0.0
_requests_this_process = 0
_reservation_lock = threading.Lock()


def _budget_from(ai_cfg: dict, name: str, default: int) -> int:
    if name not in ai_cfg:
        return default
    value = ai_cfg[name]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    log.warning("[AI] Некорректное значение ai.%s в config.json (нужно целое число от 0); "
                "использую значение по умолчанию: %d", name, default)
    return default


def budgets_from_config(ai_cfg: object) -> tuple[int, int]:
    """(суточный лимит, лимит за запуск) из раздела "ai" настроек; неверное заменяется умолчанием."""
    section = ai_cfg if isinstance(ai_cfg, dict) else {}
    daily = _budget_from(section, "daily_budget", DAILY_REQUEST_BUDGET)
    return daily, _budget_from(section, "per_run_budget", daily)


def configure_budgets(daily: int | None = None, per_run: int | None = None) -> None:
    """Задать лимиты для процесса; без аргументов — вернуть значения по умолчанию."""
    global _daily_budget, _per_run_budget
    daily = DAILY_REQUEST_BUDGET if daily is None else daily
    per_run = daily if per_run is None else per_run
    if daily < 0 or per_run < 0:
        raise ValueError("request budgets must not be negative")
    with _reservation_lock:
        _daily_budget, _per_run_budget = daily, per_run


def current_budgets() -> tuple[int, int]:
    """Действующие (суточный лимит, лимит за запуск)."""
    return _daily_budget, _per_run_budget


def requests_used_this_run() -> int:
    """Сколько запросов уже зачтено этому процессу."""
    return _requests_this_process


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
    daily_budget: int | None = None,
    per_run_budget: int | None = None,
) -> bool:
    """Persist one daily request slot before sending; fail closed on unreadable state."""
    global _requests_this_process
    daily_budget = _daily_budget if daily_budget is None else daily_budget
    per_run_budget = _per_run_budget if per_run_budget is None else per_run_budget
    if daily_budget < 0 or per_run_budget < 0:
        raise ValueError("request budgets must not be negative")

    with _reservation_lock:
        if _requests_this_process >= per_run_budget:
            log.warning(
                "[AI] Достигнут внутренний лимит приложения за запуск: %d попытки OpenRouter. "
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
        _requests_this_process = max(_requests_this_process, _per_run_budget)


def _daily_usage_count(data: object, today: str, path: Path) -> int | None:
    if not isinstance(data, dict):
        log.error("[AI] Счётчик лимита OpenRouter имеет неверный формат: %s", path)
        return None
    stored_date = data.get("date")
    if not isinstance(stored_date, str):
        log.error("[AI] Дата счётчика лимита OpenRouter имеет неверный формат: %s", path)
        return None
    try:
        stored_day = date.fromisoformat(stored_date)
    except ValueError:
        log.error("[AI] Дата счётчика лимита OpenRouter повреждена: %s", path)
        return None
    if stored_day.isoformat() != stored_date:
        log.error("[AI] Дата счётчика лимита OpenRouter имеет неверный формат: %s", path)
        return None

    today_date = date.fromisoformat(today)
    if stored_day < today_date:
        return 0
    if stored_day > today_date:
        log.error("[AI] Дата счётчика лимита OpenRouter находится в будущем: %s", path)
        return None

    used = data.get("requests")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        log.error("[AI] Счётчик запросов OpenRouter повреждён: %s", path)
        return None
    return used


def remaining_daily_requests(path: Path = USAGE_PATH, daily_budget: int | None = None) -> int | None:
    """Return today's remaining budget, or None when the ledger cannot be trusted."""
    daily_budget = _daily_budget if daily_budget is None else daily_budget
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(path):
            if not path.exists():
                return daily_budget
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("[AI] Не удалось проверить дневной лимит OpenRouter: %s", exc)
        return None

    used = _daily_usage_count(data, datetime.now(timezone.utc).date().isoformat(), path)
    if used is None:
        return None
    return max(0, daily_budget - used)


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
        used = _daily_usage_count(data, today, path)
        if used is None:
            return False
    else:
        used = 0

    if used >= daily_budget:
        log.warning(
            "[AI] Достигнут внутренний дневной лимит приложения: %d попыток OpenRouter за UTC-сутки. "
            "Сканирование продолжится без новых запросов.",
            daily_budget,
        )
        return False

    try:
        atomic_write_json(path, {"date": today, "requests": used + 1})
    except OSError as exc:
        log.error("[AI] Не удалось обновить счётчик лимита OpenRouter (%s): %s", path, exc)
        return False
    _requests_this_process += 1
    return True


def wait_for_request_slot() -> None:
    """Apply the app's minimum interval between outbound model requests."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    wait = MIN_REQUEST_INTERVAL_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _ai_section(config_path: Path) -> object:
    """Раздел "ai" из config.json; при любой ошибке чтения — пусто (лимиты по умолчанию)."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return config.get("ai") if isinstance(config, dict) else None


def progress_line(
    config_path: Path = CONFIG_PATH,
    usage_path: Path = USAGE_PATH,
    analysis_cache_path: Path = ANALYSIS_CACHE_PATH,
) -> str:
    """Строка для start.bat: «остаток суточного лимита|страниц Nexus в кэше» или «-1|-1»."""
    daily_budget, _ = budgets_from_config(_ai_section(config_path))
    remaining = remaining_daily_requests(usage_path, daily_budget)
    page_count = cached_analysis_page_count(analysis_cache_path)
    if remaining is None or page_count is None:
        return "-1|-1"
    return f"{remaining}|{page_count}"


def main(argv: list[str] | None = None) -> int:
    """`python openrouter_limits.py progress` — оставлено для совместимости со старыми сценариями."""
    import sys

    arguments = sys.argv[1:] if argv is None else argv
    if arguments != ["progress"]:
        print("Использование: python openrouter_limits.py progress", file=sys.stderr)
        return 2
    line = progress_line()
    print(line)
    return 1 if line == "-1|-1" else 0
