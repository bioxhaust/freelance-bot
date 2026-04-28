# Freelance Bot

Telegram-бот для мониторинга фриланс-бирж (Kwork, FL.ru), оценки релевантности через LLM (Ollama Cloud) и интерактивного управления откликами через inline-кнопки.

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполни секреты в .env (см. ниже)
python main.py
```

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота из @BotFather |
| `TELEGRAM_CHAT_ID` | id чата, в который слать уведомления |
| `OLLAMA_API_KEY`, `OLLAMA_MODEL` | Ollama Cloud (default: `gpt-oss:120b`) |
| `MY_SKILLS` | твоё описание стека (используется в промптах LLM) |
| `PREFILTER_INCLUDE_KEYWORDS` | через запятую — пропускать в LLM |
| `PREFILTER_EXCLUDE_KEYWORDS` | через запятую — отсекать до LLM |
| `RELEVANCE_THRESHOLD` | порог релевантности 0..100 (default 70) |
| `DRAFT_MODE` | `1` — добавлять черновик отклика к карточке |
| `DRAFT_MAX_CHARS` | лимит черновика, **жёсткий cap 1500** (Telegram limit 4096) |
| `POLL_INTERVAL_SECONDS` | интервал цикла парсинга |
| `SOURCE_MAX_PAGES` | сколько страниц ленты парсить за цикл |
| `FRESH_ORDER_MAX_AGE_MINUTES` | слать только заказы не старше N минут (`0` — выключить) |
| `KWORK_COOKIES`, `FLRU_COOKIES` | cookies из браузера (для авторизованных лент) |
| `KWORK_CATEGORIES` | id категорий через запятую (default `41,11,45`) |

## UX в Telegram

Карточка релевантного заказа имеет inline-клавиатуру с прогрессивным раскрытием:

- **До генерации черновика**: одна широкая кнопка `✏️ Сгенерировать черновик`.
- **После черновика**: `📨 Отправить` / `🔄 Перегенерить` / `✂️ Короче` / `🎭 Сменить тон` (открывает submenu `Деловой` / `Дружелюбный` / `Жёсткий`).
- **Всегда видны**: `❓ Почему` / `🌐 Открыть` / `⏭ Пропустить` / `🗄 В архив`.

После `Отправить` / `Пропустить` / `В архив` карточка редактируется на месте: добавляется статус, клавиатура заменяется на одну кнопку `🌐 Открыть заказ`. Все остальные действия (`Перегенерить`, `Короче`, `Сменить тон`) **редактируют ту же карточку**, не плодя новые сообщения в чате.

Текстовые команды (ответом на карточку): `черновик` / `короче` / `жестче` / `дружелюбнее` / `почему`.

## Архитектура

```
main.py
├── HttpClient (общий, keep-alive, retries, Retry-After)
├── Database (aiosqlite, WAL, миграции с _meta.schema_version)
├── KworkExchange (JSON API, Europe/Moscow timezone, parallel pages)
├── FLRuExchange (selectolax-парсер, parallel categories, расширенный age)
├── LLMClient (Ollama Cloud, JSON-mode для score, fallback на текст)
├── Scheduler (parallel exchanges, atomic INSERT OR IGNORE)
├── TelegramNotifier (HTML-карточка, inline keyboards)
└── TelegramAssistant (long-poll, edit-in-place, idempotent send)
```

## Smoke-тест

```bash
source .venv/bin/activate
python _smoke.py
```

Проверяет: миграция БД, schema verification, html.escape, _parse_score (last/json), DRAFT_MAX_CHARS cap, FL.ru selectolax-парсер на golden-fixture, Kwork JSON-парсер на 6 категориях.
