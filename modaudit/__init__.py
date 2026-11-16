"""Cyberpunk 2077 Mod Auditor: модули программы.

Запускаемые файлы (`nexus_links.py`, `server.py`, `start.bat`) и файлы пользователя
(config.json, кэши, отчёт) лежат в папке проекта — на уровень выше этого пакета.
"""

from pathlib import Path

# Папка проекта: здесь лежат настройки и кэши пользователя, а не внутри пакета.
PROJECT_DIR = Path(__file__).resolve().parent.parent
