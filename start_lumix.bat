@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Подключение к Wi-Fi камеры...
netsh wlan connect name="TZ100-0B51C1" >nul 2>&1

echo [2/3] Открытие интерфейса в браузере...
start "" "http://127.0.0.1:8000"

echo [3/3] Запуск сервера FastAPI...
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause