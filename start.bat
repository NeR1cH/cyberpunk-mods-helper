@echo off
chcp 65001 > nul
title Cyberpunk 2077 Mod Auditor

cd /d "%~dp0"

echo ======================================================
echo  Cyberpunk 2077 Mod Auditor
echo ======================================================
echo.

:: 1. Проверка Python: сначала python, затем лаунчер py -3; нужна версия 3.10 или новее
set "PY=python"
python --version > nul 2>&1
if errorlevel 1 set "PY=py -3"
%PY% --version > nul 2>&1
if errorlevel 1 (
    echo [!] Ошибка: Python не обнаружен ^(ни python, ни py -3^).
    echo     Установите Python 3.10+ с галочкой "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [!] Ошибка: нужен Python 3.10 или новее, а найден:
    %PY% --version
    echo.
    pause
    exit /b 1
)
echo [+] Python обнаружен: %PY%

:: 2. Создание config.json из примера
set "CONFIG_CREATED=0"
if not exist "config.json" (
    if exist "config.example.json" (
        copy "config.example.json" "config.json" > nul
        if errorlevel 1 (
            echo [!] Ошибка: не удалось создать config.json.
            pause
            exit /b 1
        )
        set "CONFIG_CREATED=1"
        echo [+] Создан файл config.json на основе config.example.json
        echo     Важно: откройте config.json и проверьте пути/API-ключ.
    ) else (
        echo [!] Ошибка: config.example.json не найден!
        pause
        exit /b 1
    )
) else (
    echo [*] config.json уже существует, перезапись пропущена.
)

:: 3. Создание базового файла кэша ИИ
if not exist "ai_cache.json" (
    echo {} > "ai_cache.json"
    echo [+] Создан пустой ai_cache.json
) else (
    echo [*] ai_cache.json уже существует.
)

:: 4. Создание базового черного списка
if not exist "blacklist.json" (
    echo {} > "blacklist.json"
    echo [+] Создан пустой blacklist.json
) else (
    echo [*] blacklist.json уже существует.
)

if "%CONFIG_CREATED%"=="1" (
    echo.
    echo [!] Укажите реальные пути в config.json и запустите start.bat снова.
    pause
    exit /b 0
)

echo.
echo ======================================================
echo  Сканирование архивов и создание дашборда...
echo  ИИ обрабатывает партии в одном запуске, пока хватает дневного лимита.
echo  Внутренний лимит приложения: ai.daily_budget в config.json, по умолчанию 10 попыток за UTC-сутки (не квота OpenRouter).
echo ======================================================
echo.

%PY% nexus_links.py scan --no-open --ai-auto --ai-until-budget --ai-max-requests 1 --nexus-analyze --nexus-ai-max-mods 2 --nexus-max-requests 20
if errorlevel 1 (
    echo.
    echo [!] Сканирование не удалось.
    echo     Проверьте пути downloads и vortex в config.json,
    echo     убедитесь, что папки существуют, и запустите start.bat снова.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo  Запуск дашборда на http://localhost:8000
echo  Для завершения нажмите Ctrl+C.
echo ======================================================
echo.
%PY% server.py