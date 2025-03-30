#!/bin/bash

# Запуск FastAPI сервера в фоновом режиме
uvicorn main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

# Ждем 5 секунд, чтобы сервер успел запуститься и создать таблицу
echo "Ожидание запуска FastAPI сервера..."
sleep 5

# Запуск телеграм-бота
echo "Запуск Telegram бота..."
python bot.py 