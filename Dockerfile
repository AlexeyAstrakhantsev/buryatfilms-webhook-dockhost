FROM python:3.11-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Создаем директорию для данных
RUN mkdir -p /mount/database && chmod 777 /mount/database

# Запускаем скрипт для запуска обоих сервисов
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"] 