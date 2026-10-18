@echo off
chcp 65001 >nul
rem Запуск оверлея из исходников (нужен Python 3.11+ и установленные зависимости).
rem Установка:  pip install -e ".[vision,overlay,ru]"
rem Клавиша по умолчанию: Shift+D (показывает цены у открытой панели наград).
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0src
set ALDUR_APPRAISER_DATA_DIR=%~dp0runtime

where py >nul 2>nul
if %errorlevel%==0 (
    py -u -m aldur_appraiser.ru_app %*
) else (
    python -u -m aldur_appraiser.ru_app %*
)

if errorlevel 1 (
    echo.
    echo Не удалось запустить. Проверьте, что установлен Python и зависимости:
    echo   pip install -e ".[vision,overlay,ru]"
    pause
)
endlocal
