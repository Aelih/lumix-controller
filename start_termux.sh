#!/data/data/com.termux/files/usr/bin/bash
pkill -f "uvicorn app.main:app"
cd "$(dirname "$0")"
nohup uvicorn app.main:app --host 127.0.0.1 --port 8000 > /dev/null 2>&1 &
sleep 1
am start -a android.intent.action.VIEW -d "http://127.0.0.1:8000"