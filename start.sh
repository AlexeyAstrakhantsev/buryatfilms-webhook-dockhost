#!/bin/bash

# Запуск FastAPI сервера в фоновом режиме
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Запуск телеграм-бота
python bot.py 