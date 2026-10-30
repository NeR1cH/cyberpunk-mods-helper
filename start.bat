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
echo ======================================================
echo.

python nexus_links.py scan --no-open --ai-auto
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
python server.py