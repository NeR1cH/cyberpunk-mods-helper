"""Совместимость: код переехал в modaudit.limits.

`python openrouter_limits.py progress` работает как раньше.
"""
from modaudit import limits
from modaudit.limits import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(limits.main())
