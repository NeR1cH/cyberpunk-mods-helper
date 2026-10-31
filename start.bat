@echo off
chcp 65001 > nul
title Cyberpunk 2077 Mod Auditor

cd /d "%~dp0"

echo ======================================================
echo  Cyberpunk 2077 Mod Auditor
echo ======================================================
echo.

:: 1. Проверка Python
python --version > nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [!] Ошибка: Python не обнаружен в PATH.
    echo     Установите Python 3.10+ с галочкой "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)
echo [+] Python обнаружен.

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
echo  Автоматическая обработка партиями, пока хватает дневного лимита.
echo  Безопасный общий лимит OpenRouter: 10 попыток за UTC-сутки.
echo ======================================================
echo.

:scan_loop
set "AI_REQUESTS_BEFORE="
set "NEXUS_PAGES_BEFORE="
for /f "tokens=1,2 delims=|" %%R in ('python openrouter_limits.py progress') do (
    set "AI_REQUESTS_BEFORE=%%R"
    set "NEXUS_PAGES_BEFORE=%%S"
)

python nexus_links.py scan --no-open --ai-auto --ai-max-requests 1 --nexus-analyze --nexus-ai-max-mods 2 --nexus-max-requests 20
if errorlevel 1 (
    echo.
    echo [!] Сканирование не удалось.
    echo     Проверьте пути downloads и vortex в config.json,
    echo     убедитесь, что папки существуют, и запустите start.bat снова.
    pause
    exit /b 1
)

set "AI_REQUESTS_AFTER="
set "NEXUS_PAGES_AFTER="
for /f "tokens=1,2 delims=|" %%R in ('python openrouter_limits.py progress') do (
    set "AI_REQUESTS_AFTER=%%R"
    set "NEXUS_PAGES_AFTER=%%S"
)
if not defined AI_REQUESTS_BEFORE goto scan_complete
if not defined AI_REQUESTS_AFTER goto scan_complete
if not defined NEXUS_PAGES_BEFORE goto scan_complete
if not defined NEXUS_PAGES_AFTER goto scan_complete
if "%AI_REQUESTS_AFTER%"=="-1" goto scan_complete
if "%AI_REQUESTS_BEFORE%"=="%AI_REQUESTS_AFTER%" if "%NEXUS_PAGES_BEFORE%"=="%NEXUS_PAGES_AFTER%" goto scan_complete
if "%AI_REQUESTS_AFTER%"=="0" goto scan_complete
echo.
echo [*] Партия завершена. Осталось запросов OpenRouter на сегодня: %AI_REQUESTS_AFTER%.
echo [*] Перехожу к следующей партии...
echo.
timeout /t 4 /nobreak > nul
goto scan_loop

:scan_complete
echo.
echo ======================================================
echo  Запуск дашборда на http://localhost:8000
echo  Для завершения нажмите Ctrl+C.
echo ======================================================
echo.
python server.py