@echo off
chcp 65001 > nul
title Инициализация окружения Cyberpunk 2077 Mod Auditor

cd /d "%~dp0"

echo ======================================================
echo  Настройка проекта Cyberpunk 2077 Mod Auditor
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
if not exist "config.json" (
    if exist "config.example.json" (
        copy "config.example.json" "config.json" > nul
        if errorlevel 1 (
            echo [!] Ошибка: не удалось создать config.json.
            pause
            exit /b 1
        )
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

echo.
echo ======================================================
echo  Инициализация успешно завершена!
echo  Отредактируйте config.json и запустите start.bat.
echo ======================================================
echo.
pause