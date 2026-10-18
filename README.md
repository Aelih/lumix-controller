# Lumix DMC-TZ100 Web Controller

Веб-интерфейс на FastAPI для управления камерой Panasonic Lumix DMC-TZ100 по Wi-Fi, просмотра Live View (UDP MJPEG) и скачивания оригиналов фото (RAW/JPG) и 4K-видео через DLNA.

## Запуск
1. Скопируйте `.env.example` в `.env` и укажите IP камеры.
2. Установите зависимости: `pip install -r requirements.txt`
3. Запустите сервер: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
4. Откройте `http://localhost:8000` (Swagger UI доступен на `/docs`).