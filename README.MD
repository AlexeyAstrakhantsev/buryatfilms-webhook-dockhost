# Telegram Бот для Управления Подпиской

Бот для автоматизации подписки на закрытый Telegram-канал с использованием платежной системы LAVA.TOP.

## 🚀 Возможности

- Автоматическое управление подписками на канал
- Прием платежей через LAVA.TOP
- Различные периоды подписки (1 месяц, 3 месяца, 6 месяцев, 1 год)
- Автоматическое продление подписки
- Возможность отмены автопродления
- Система уведомлений об окончании подписки
- Административная статистика

## 📋 Требования

- Python 3.11+
- Docker (опционально)
- Telegram Bot Token
- LAVA.TOP API ключ
- Закрытый Telegram канал

## 🛠 Установка

### Через Docker (рекомендуется)

1. Клонируйте репозиторий:

```bash
git clone https://github.com/yourusername/telegram-bot.git
cd telegram-bot
```

2. Создайте файл с настройками:

```bash
cp .env.example .env
```

3. Отредактируйте `.env` файл, добавив свои значения

4. Запустите через Docker:
```bash
docker build -t subscription-bot .
docker run -d --name subscription-bot --env-file .env -v $(pwd)/data:/mount/database subscription-bot
```

### Без Docker

1. Установите зависимости:
```bash
pip install -r app/requirements.txt
```

2. Настройте переменные окружения (скопируйте `.env.example` в `.env` и заполните)

3. Запустите бота:
```bash
./start.sh
```

## ⚙️ Настройка

### Необходимые переменные окружения:

- `BOT_TOKEN` - Токен вашего Telegram бота
- `CHANNEL_ID` - ID закрытого канала
- `ADMIN_ID` - Telegram ID администратора
- `LAVA_API_KEY` - API ключ от LAVA.TOP
- `SUPPORT_USERNAME` - Username службы поддержки
- `CHANNEL_LINK` - Ссылка на канал
- `WEBHOOK_USERNAME` и `WEBHOOK_PASSWORD` - Данные для доступа к вебхуку
- `MAIN_MESSAGE` - Текст приветственного сообщения

## 👨‍💻 Команды администратора

- `/stat` - Показать статистику подписок
- `/reset_db` - Сбросить базу данных
- `/test` - Тестовый платеж
- `/test_fail` - Тестовый неуспешный платеж
- `/test_expire` - Тестовая истекшая подписка

## 📱 Команды пользователя

- `/start` - Начать работу с ботом
- `/subscribe` - Оформить подписку
- `/status` - Проверить статус подписки

## 🔄 API Endpoints

- `POST /lava/payment` - Вебхук для уведомлений от LAVA.TOP
- `POST /admin/reset_db` - Эндпоинт для сброса базы данных

## 📁 Структура проекта

```
.
├── app/
│   ├── bot.py          # Основной код бота
│   ├── main.py         # FastAPI сервер для вебхуков
│   └── requirements.txt # Зависимости проекта
├── data/               # Директория для базы данных и логов
├── .env.example        # Пример файла с настройками
├── Dockerfile          # Конфигурация Docker
├── README.md          # Документация
└── start.sh           # Скрипт запуска
```

## 📝 Логирование

Логи сохраняются в директории `/mount/database/`:
- `bot_YYYYMMDD.log` - Логи бота
- `lava_webhook_YYYYMMDD.log` - Логи вебхука

## 🤝 Поддержка

По всем вопросам обращайтесь к администратору бота через команду поддержки в боте.

## 📜 Лицензия

MIT
