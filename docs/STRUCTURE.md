# Структура проекта: AI-помощник менеджера для amoCRM

Связанные документы: [TZ.md](TZ.md) (цели, требования, приёмка), [FUNCTIONALITY.md](FUNCTIONALITY.md) (сценарии и логика).

**Статус:** этапы 1 и 2 реализованы. Интеграция с amoCRM проверена в mock-режиме: поддельный сервер amoCRM, а не живой аккаунт. Элементы с пометкой «этап 3» — план развития, структура оставляет для них место.

## 1. Принципы

- **Ядро не знает, откуда пришло обращение.** CLI, REST, демо-страница и amoCRM — адаптеры вокруг одной функции `Assistant.suggest()`.
- **Внешние сервисы спрятаны за интерфейсами** `LLMClient` и `AmoClient`. У LLM есть mock-реализация. У amoCRM — поддельный сервер, против которого работает настоящий клиент, поэтому демо и тесты обходятся без ключей и аккаунтов.
- **Всё изменяемое лежит вне кода.** БЗ — в `knowledge_base/`, настройки — в переменных окружения, тексты промпта — константами в `app/core/prompts.py`.
- **Минимум инфраструктуры.** Один процесс, SQLite, фоновый обработчик очереди на asyncio. Redis, Celery и векторная БД для этого объёма не нужны.

## 2. Архитектура

```
Клиент (Telegram / WhatsApp / чат на сайте)
   │
   ▼
amoCRM: чат сделки ── вебхуки add_message / add_outgoing_message ──┐
   ▲                                                               ▼
   │                                     POST /webhooks/amocrm/{secret}
   │                                     разбор, дедуп, запись в БД — ~10 мс
   │                                                               │
   │                                     Обработчик очереди: пауза 6 с → контекст
   │                                       ├ история диалога (своя БД)
   │                                       ├ сделка, контакт, этап, товары: API v4
   │                                       └ база знаний (целиком, кэш)
   │                                                               │
   │                                     LLM (structured output, 2 блока):
   │                                       Claude через Anthropic API или любая из
   │                                       6 моделей через OpenAI-совместимый шлюз
   │                                                               │
   │                                     Проверки: цены, ссылки на БЗ, стоп-фразы
   │                                                               │
   ├──── примечание service_message в сделку ◄─────────────────────┤
   └──── виджет (правая панель) ◄── GET /api/v1/leads/{id}/suggestion   (этап 3)

CLI ─────────────────┐
Демо-страница ───────┼──► Assistant.suggest() ──► то же ядро (без очереди)
POST /api/v1/suggest ┘
```

В mock-режиме место amoCRM занимает поддельный сервер (`app/amocrm/fake.py`), а сообщения клиента присылает имитатор (`python -m app.amocrm.simulate`).

## 3. Стек

| Назначение | Выбор |
|---|---|
| Язык | Python 3.11+ (образ Docker — 3.12) |
| HTTP-сервис | FastAPI + Uvicorn |
| LLM | Claude — `anthropic` 1.x (официальный SDK, импортируется, только когда Claude идёт через Anthropic API), модель `claude-opus-5-5`. GLM-5.3, DeepSeek V4 Pro, Kimi K3, Qwen3.8-Max, Grok 4.7 (и Claude без ключа Anthropic) — через OpenAI-совместимый шлюз (New API, OpenRouter, LiteLLM) по httpx |
| HTTP-клиент amoCRM | httpx (async); в mock-режиме и тестах — `httpx.MockTransport` с поддельным сервером |
| Схемы и настройки | Pydantic v2, pydantic-settings |
| БЗ | PyYAML + Markdown-файлы |
| Хранилище | SQLite через `aiosqlite` |
| Демо-страница | Jinja2 + vanilla JS, без сборки |
| Тесты | pytest, pytest-asyncio |
| Линтер и форматирование | ruff |
| Запуск | Docker + docker-compose; для вебхуков из живого amoCRM — HTTPS-туннель (cloudflared, а если туннели Cloudflare блокирует провайдер — SSH-туннель pinggy). Онлайн-демо — Vercel: всё приложение одной функцией, mock-режим |
| Проверка JWT виджета и Salesbot (этап 3) | PyJWT |
| Похожесть текстов для автооценки черновиков (этап 3) | rapidfuzz |

## 4. Дерево каталогов

```
Testovoe_O_Complex/
├── app/
│   ├── main.py                  # create_app(): lifespan (БЗ, БД, LLM, amoCRM, обработчик), request-id, роуты
│   ├── config.py                # Settings из переменных окружения и .env; проверка настроек amoCRM
│   ├── logging_setup.py         # JSON-логи с request_id
│   ├── scenarios.py             # сценарии демо и имитатора (examples/scenarios)
│   ├── cli.py                   # CLI-скрипт (F-01)
│   ├── setup_gateway.py         # адрес и ключ шлюза LLM в .env с проверкой ключа и моделей
│   ├── envfile.py               # запись в .env и скрытый ввод секретов для команд подключения
│   ├── core/
│   │   ├── assistant.py         # Assistant: подготовка запроса → LLM с повторами → проверки → результат
│   │   ├── prompts.py           # системный промпт, user-сообщение, защита тегов разметки
│   │   ├── schemas.py           # SuggestRequest, DialogMessage, LeadContext, Suggestion, Upsell, Meta
│   │   ├── llm.py               # интерфейс LLMClient, AnthropicLLMClient, ошибки LLM
│   │   ├── providers.py         # модели для переключения и их особенности (формат, рассуждения)
│   │   ├── gateway_llm.py       # GatewayLLMClient: модель через OpenAI-совместимый шлюз
│   │   ├── llm_registry.py      # LLMRegistry: какой клиент за какую модель, запасные модели
│   │   ├── mock_llm.py          # MockLLMClient: заранее составленные ответы + поиск по FAQ
│   │   ├── guards.py            # проверки и исправления результата
│   │   ├── money.py             # формат «32 900 ₽» и извлечение сумм из текста
│   │   └── pii.py               # маскирование телефонов, e-mail, карт
│   ├── kb/
│   │   ├── models.py            # Product, FaqItem, Policy, UpsellRule, KnowledgeBase
│   │   └── loader.py            # чтение, проверка, вывод в промпт, хэш версии, KnowledgeStore
│   ├── amocrm/
│   │   ├── webhooks.py          # разбор вебхуков (form с вложенными ключами или JSON) → ChatMessageEvent
│   │   ├── client.py            # AmoClient: API v4, лимит запросов/с, повторы, разбор ответов
│   │   ├── context.py           # сделка, этап, товары, имя клиента, куда писать примечание
│   │   ├── notes.py             # тексты примечаний: подсказка, вложение, сбой
│   │   ├── payloads.py          # сообщение «как из amoCRM»: тело вебхука для имитатора и страницы mock
│   │   ├── fake.py              # поддельный сервер amoCRM для mock-режима и тестов
│   │   ├── factory.py           # сборка клиента по настройкам, проверка токена
│   │   ├── simulate.py          # консольный имитатор amoCRM
│   │   ├── connect.py           # подключение живого аккаунта: адрес и токен в .env с проверкой токена
│   │   └── setup_webhook.py     # регистрация вебхука в живом amoCRM
│   ├── worker/
│   │   └── processor.py         # Inbox (приём) и Worker (очередь): пауза, stale, повторы, публикация
│   ├── storage/
│   │   ├── db.py                # подключение SQLite, схема, формат времени
│   │   └── repo.py              # SuggestionRepo, DialogRepo, JobRepo, очистка по сроку хранения
│   ├── api/
│   │   ├── deps.py              # доступ к объектам приложения, проверка API_TOKEN / ADMIN_TOKEN
│   │   ├── errors.py            # единый формат ошибок
│   │   ├── suggest.py           # POST /api/v1/suggest, GET /api/v1/suggestions/{id}
│   │   ├── kb.py                # GET /api/v1/kb, POST /api/v1/kb/reload
│   │   ├── health.py            # GET /health
│   │   ├── demo.py              # GET / и /amocrm (страницы), GET /api/v1/demo/scenarios
│   │   ├── llm.py               # GET /api/v1/llm/providers — модели и их состояние
│   │   ├── live.py              # живая модель за паролем: POST /api/v1/live/login, X-Live-Password
│   │   ├── webhooks.py          # POST /webhooks/amocrm/{secret}
│   │   ├── amocrm_mock.py       # /api/v1/amocrm-mock: notes, leads, feed, messages (только mock-режим)
│   │   ├── widget.py            # GET /api/v1/leads/{id}/suggestion, regenerate (этап 3)
│   │   ├── feedback.py          # POST /api/v1/suggestions/{id}/feedback (этап 3)
│   │   ├── stats.py             # GET /api/v1/stats (этап 3)
│   │   └── salesbot.py          # POST /salesbot/amocrm/{secret} (этап 3)
│   └── web/
│       ├── templates/           # _header.html (шапка с навигацией), index.html — прямой вызов (F-02),
│       │                        # amocrm.html — карточка и лента сделки поддельного amoCRM
│       └── static/              # common.js, app.js, amocrm.js, styles.css
├── knowledge_base/              # демо-БЗ «Климат-Демо» (формат — раздел 10)
├── examples/
│   ├── dialog.json, lead.json   # вход для CLI
│   ├── scenarios/               # 8 готовых сценариев: демо-страницы и имитатор amoCRM
│   ├── mock_llm/                # ответы для mock-режима: 6 — записанные ответы Grok 4.7, 2 — вручную (README там)
│   └── amocrm/mock_account.json # «аккаунт» поддельного amoCRM: воронка, сделки, контакты, каталог
├── tests/                       # раздел 12
├── evals/                       # раздел 12
├── widget/                      # виджет amoCRM, упаковывается в zip (этап 3)
├── docs/                        # TZ, FUNCTIONALITY, STRUCTURE, AI_USAGE; screenshots/ — для README
├── data/                        # SQLite (в .gitignore, volume в Docker)
├── .env.example
├── .gitignore, .dockerignore
├── Dockerfile, docker-compose.yml
├── .github/workflows/ci.yml     # GitHub Actions: ruff и pytest (Python 3.11, 3.12), сборка и запуск образа
├── vercel.json                  # онлайн-демо: регион, mock-режимы, что не попадает в функцию
├── pyproject.toml               # зависимости, ruff, pytest, команда ai-assistant, точка входа для Vercel
└── README.md
```

## 5. Модули и ответственность

| Модуль | Отвечает за | Зависит от |
|---|---|---|
| `core/assistant.py` | `prepare_request()`: обрезка истории до `HISTORY_LIMIT`, маскирование ПДн в обращении, истории и `replies`. `suggest(request, mode, provider)`: промпты, вызов модели с повторами (обрезанный ответ — один раз с удвоенным лимитом; невалидный или пустой — ещё одна попытка), при сбое модели по умолчанию — запасные с предупреждением `llm_fallback`, проверки, `meta` (с `provider`), лог | `kb`, `llm_registry`, `guards`, `pii`, `prompts` |
| `core/prompts.py` | Системный промпт (правила + тон + вся БЗ) и user-сообщение (`<lead>`, `<history>`, `<new_message>`, `<replies>`, `<task>`). В пользовательском тексте «ломаются» теги разметки, чтобы клиент не мог закрыть `<new_message>` | `kb` |
| `core/schemas.py` | Pydantic-модели входа и выхода ядра. Описания полей `Suggestion` попадают в JSON-схему для модели | — |
| `core/llm.py` | `AnthropicLLMClient`: `beta.messages.create` с `output_config` (effort + JSON-схема), `cache_control` на системном промпте, `fallbacks="default"`. Проверка учётных данных до запроса, разбор `stop_reason` (`refusal`, `max_tokens`) до чтения ответа, валидация JSON своей моделью, учёт `usage`, перевод ошибок SDK в `LLMUnavailableError` / `LLMRefusedError` / `LLMTruncatedError` / `LLMBadOutputError`. SDK импортируется лениво | `anthropic` |
| `core/providers.py` | `ProviderSpec` для шести моделей: какой `response_format` просить, уровни `reasoning_effort`, дополнительные поля (`enable_thinking: false` у Qwen); `map_effort()` приводит `LLM_EFFORT` к уровням модели | `config` |
| `core/gateway_llm.py` | `GatewayLLMClient`: `POST {LLM_GATEWAY_URL}/chat/completions`, схема ответа и в `response_format`, и в системном промпте; упрощение запроса на 400 (без рассуждений → `json_object` → без формата) с запоминанием; повторы на 429/5xx/сетевых ошибках; ошибки ключа, баланса, модели и адреса — без повторов, с подсказкой настройки; разбор `finish_reason` до текста, извлечение JSON из `<think>` и ```` ``` ````, `usage` в формате Anthropic | httpx, `providers`, `llm` |
| `core/llm_registry.py` | `build_llms()`: в mock-режиме на все модели отвечает `MockLLMClient`; в live — Claude через Anthropic API (если есть ключ или шлюз не настроен), остальные через шлюз. `LLMRegistry`: модель по умолчанию, запасные (`chain()`), `describe()` для API и страницы, `check_gateway()` — при старте отмечает недоступными модели, которых нет в `GET /models` шлюза | `llm`, `gateway_llm`, `mock_llm` |
| `core/mock_llm.py` | Ответ по совпадению текста обращения с записью из `examples/mock_llm/`, иначе — по похожему вопросу FAQ, иначе — «уточню» с `needs_human` | `llm`, `kb` |
| `core/guards.py` | Проверки из [FUNCTIONALITY.md, 3.7](FUNCTIONALITY.md#37-проверки-результата-f-06): суммы и их формат, ссылки, товары, допродажа при жалобе и в ответе клиенту, внутренние термины, стоп-фразы, длина | `kb`, `money` |
| `core/money.py` | `format_rub()` и `extract_amounts()` — суммы только с явной валютой (₽, руб., р.) | — |
| `core/pii.py` | Маскирование телефонов (РФ и международных), e-mail, номеров карт (с проверкой Луна) | — |
| `kb/models.py` | Схемы записей БЗ, `KnowledgeBase` с индексами | — |
| `kb/loader.py` | Чтение файлов, проверка (все ошибки сразу, с файлом, записью и полем), фиксированный вывод в промпт, хэш версии, атомарная перезагрузка в `KnowledgeStore` | `kb/models` |
| `amocrm/webhooks.py` | Разбор `message[add]` (клиент) и `outgoing_message[add]` (сотрудник с `user_id` или бот) в `ChatMessageEvent`; ключ диалога — `chat_id`, иначе `talk_id` или `contact_id`. Обратное кодирование для имитатора | — |
| `amocrm/client.py` | Методы API v4 (раздел 9), `Authorization: Bearer`, не больше `AMOCRM_RPS` запросов/с, повторы на 429/5xx/сетевых ошибках с растущей паузой и учётом `Retry-After`; 204 и 404 на чтение — «не найдено»; кэш воронок (1 ч) и названий товаров | httpx |
| `amocrm/context.py` | Выбор сделки: из привязки чата или из сделок контакта (открытая, обновлённая последней, иначе последняя закрытая); сборка `LeadContext` и цели примечания | `client` |
| `amocrm/notes.py` | Тексты примечаний: подсказка (с черновиком или без), вложение без текста, сбой | — |
| `amocrm/fake.py` | Поддельный amoCRM: маршруты API v4 в формате документации, проверка токена, 204 для ненайденного, 400 при примечании к несуществующей сущности; примечания хранит в памяти | httpx |
| `amocrm/factory.py` | Клиент по `AMOCRM_MODE`: `live` — настоящий API, `mock` — поверх поддельного сервера; проверка токена при старте | `client`, `fake` |
| `amocrm/payloads.py` | Сообщение и тело вебхука в формате amoCRM — общие для имитатора и страницы «amoCRM (mock)» | `webhooks` |
| `amocrm/simulate.py`, `amocrm/setup_webhook.py`, `amocrm/connect.py` | Консольные инструменты: имитатор вебхуков, регистрация вебхука в живом amoCRM (с проверкой адреса через `/health` и пустую посылку, повтором при «Invalid URL»), подключение аккаунта (адрес и токен в `.env`, проверка токена через `GET /api/v4/account`, `AMOCRM_ACCOUNT_ID`, случайный `WEBHOOK_SECRET`) | `payloads`, `factory`, `envfile` |
| `worker/processor.py` | `Inbox.ingest()`: запись сообщений и постановка задач (пауза перед генерацией). `Worker`: фоновый цикл, задачи с `run_at ≤ now` (до `WORKER_CONCURRENCY` одновременно), выбор `full` / `upsell_only` / `skip`, контекст, ядро, проверка на устаревание, публикация, повторы, примечание о сбое, очистка по сроку хранения | `core`, `amocrm`, `storage` |
| `storage/db.py`, `storage/repo.py` | Схема SQLite, операции с `suggestions`, `dialogs`, `messages`, `jobs`, очистка | `aiosqlite` |
| `scenarios.py` | Модель и загрузка сценариев (без FastAPI и SDK — её импортирует имитатор) | `core/schemas` |
| `api/*` | HTTP-роуты, проверка токенов, единый формат ошибок | `core`, `storage`, `worker` |
| `web/*` | Страницы демо: прямой вызов (`/`) и карточка сделки поддельного amoCRM (`/amocrm`); общий JS — `common.js` | `api` |
| `cli.py` | Разбор аргументов (включая `--provider`), вызов ядра, вывод, коды возврата | `core` |
| `setup_gateway.py` | Команда подключения шлюза: ключ вводится скрыто или из stdin, проверяется запросом `GET /models` (токены не тратятся), сохраняется в `.env` вместе с адресом; показывает, каких моделей помощника нет в шлюзе | httpx, `config` |
| `config.py` | Settings (раздел 11); относительные пути считаются от корня проекта; `amocrm_config_errors()` | pydantic-settings |
| `logging_setup.py` | JSON-формат логов, `request_id` из middleware | — |
| `main.py` | Сборка приложения и его жизненный цикл: неполные настройки amoCRM — сервис не стартует; в режиме `live` — проверка моделей шлюза | всё |

## 6. Путь обработки обращения

### 6.1 CLI, REST, демо-страница (синхронно)
1. `cli.py` или `api/suggest.py` собирает `SuggestRequest`.
2. `Assistant.suggest()`:
   - `prepare_request()` обрезает историю и маскирует ПДн;
   - системный промпт берётся из кэша по версии БЗ, user-сообщение собирается заново;
   - `llm.generate()` вызывает выбранную модель (или модель по умолчанию, а при её сбое — запасные), при необходимости с повторами;
   - `apply_guards()` проверяет и исправляет результат;
   - возвращается `SuggestResult`: `suggestion` + `meta`.
3. REST и демо сохраняют результат в `suggestions` вместе с подготовленным (замаскированным) запросом: его можно найти по id, а на этапе 3 к нему привяжется обратная связь. CLI ничего не сохраняет.

### 6.2 amoCRM (асинхронно)
1. `api/webhooks.py`: проверка секрета и аккаунта → `amocrm/webhooks.py` разбирает тело → `Inbox.ingest()` сохраняет диалог и сообщения (дубль по `amo_id` пропускается) → ответ 200.
2. Для каждого нового сообщения клиента `JobRepo.schedule()` ставит задачу диалога или сдвигает ожидающую: `run_at = now + DEBOUNCE_SECONDS`. Pending-задача на диалог всегда одна (частичный уникальный индекс).
3. `Worker` раз в `WORKER_POLL_SECONDS` забирает созревшие задачи (`pending → running`, `attempts + 1`). Параллельно — не больше `WORKER_CONCURRENCY`.
4. История диалога читается по времени amoCRM и делится на историю, новое обращение (подряд идущие последние сообщения клиента) и `replies` (что отправили после). Если среди `replies` есть реплика менеджера — режим `ON_MANAGER_REPLIED`, иначе `full`.
5. `amocrm/context.py`: сделка, этап, товары, имя клиента, цель примечания.
6. Только вложения без текста — примечание об этом, модель не вызывается.
7. Если у задачи уже есть подсказка для этого же обращения, ещё не опубликованная (повтор после сбоя записи), она берётся из `suggestions`. Иначе вызывается `Assistant.suggest()`, результат сохраняется, его id пишется в задачу.
8. Диалог перечитывается: новое сообщение клиента — задача `stale`, без публикации; ответ менеджера — примечание без черновика.
9. `AmoClient.add_note()` пишет служебное примечание, `note_id` сохраняется в подсказку, задача — `done`.
10. Ошибка LLM или amoCRM: повтор через `JOB_RETRY_SECONDS`, всего `JOB_MAX_ATTEMPTS` попыток (отказ модели — без повторов), затем `failed` и примечание о сбое. Если у диалога уже есть новая задача, старая получает `stale`.

При старте задачи в статусе `running` (прерванные перезапуском) возвращаются в очередь. Раз в сутки обработчик удаляет сообщения, подсказки и завершённые задачи старше `RETENTION_DAYS`.

Ограничение: обработчик живёт в процессе приложения, поэтому Uvicorn запускается с одним worker-процессом. Для горизонтального масштабирования понадобится внешняя очередь, в MVP она не нужна.

## 7. HTTP API сервиса

| Метод | Путь | Доступ | Назначение |
|---|---|---|---|
| POST | `/api/v1/suggest` | Открыт; если задан `API_TOKEN` — `Authorization: Bearer` | Ядро: обращение → два блока. `?provider=claude\|glm\|deepseek\|kimi\|qwen\|grok` — выбрать модель (без запасных); без параметра — `LLM_PROVIDER` и запасные |
| POST | `/api/v1/live/login` | Открыт; включён, только если задан `LIVE_DEMO_PASSWORD` и `LLM_MODE=mock` | `{password}` → `{default, providers}` или `401` (неверный пароль), `429` (5 неверных за 10 минут с одного адреса), `404` (живое демо выключено). С верным паролем в заголовке `X-Live-Password` запрос `/api/v1/suggest` уходит в живые модели; `429` — исчерпан `LIVE_DEMO_DAILY_LIMIT` |
| GET | `/api/v1/llm/providers` | Как у `/suggest` | Модели для переключения: `{mode, default, fallbacks, gateway_check, providers: [{id, name, model, route, available, problem, default}]}`; `gateway_check` — `ok`, текст ошибки проверки шлюза или `null` |
| GET | `/api/v1/suggestions/{id}` | Как у `/suggest` | Сохранённый результат с замаскированным запросом |
| GET | `/api/v1/kb` | `ADMIN_TOKEN`, если задан | Версия и содержимое БЗ |
| POST | `/api/v1/kb/reload` | `ADMIN_TOKEN`, если задан | Перезагрузка БЗ: `200 {version, changed, counts}` или `422 kb_invalid` со списком ошибок |
| GET | `/api/v1/demo/scenarios` | Открыт | Готовые сценарии демо |
| GET | `/` | Открыт | Демо-страница: прямой вызов |
| GET | `/amocrm` | Открыт | Страница «amoCRM (mock)»: карточка и лента сделки (в других режимах — подсказка, как включить) |
| GET | `/health` | Открыт | Состояние сервиса (ниже) |
| POST | `/webhooks/amocrm/{secret}` | Секрет в пути (`WEBHOOK_SECRET`) + сверка `account[id]` с `AMOCRM_ACCOUNT_ID`, если он задан | Вебхук amoCRM: `200 {ok, accepted, duplicates, scheduled, skipped}`; `404` — неверный секрет или `AMOCRM_MODE=off` |
| GET | `/api/v1/amocrm-mock/notes` | Только `AMOCRM_MODE=mock` | Примечания поддельного amoCRM: `?entity_type=leads&entity_id=1234&after_id=0` |
| GET | `/api/v1/amocrm-mock/leads` | Только `AMOCRM_MODE=mock` | Сделки поддельного аккаунта: воронка, этап, бюджет, контакт, теги, товары |
| POST | `/api/v1/amocrm-mock/messages` | Только `AMOCRM_MODE=mock` | `{lead_id, chat_id, text, direction: in\|out, author_name?, created_at?}` → тело вебхука в формате amoCRM → настоящий разбор и очередь. Секрет вебхука в браузер не попадает |
| GET | `/api/v1/amocrm-mock/feed` | Только `AMOCRM_MODE=mock` | `?lead_id=&chat_id=&after_note_id=` → лента: сообщения чата и примечания по времени, у примечания — `draft` и `pitch` из сохранённой подсказки; состояние задачи диалога (`status`, `seconds_left`) |
| GET | `/api/v1/leads/{lead_id}/suggestion` | `X-Auth-Token` (виджет) | Последняя актуальная подсказка по сделке (этап 3) |
| POST | `/api/v1/leads/{lead_id}/regenerate` | `X-Auth-Token` | Немедленная перегенерация, `202 {job_id}` (этап 3) |
| POST | `/api/v1/suggestions/{id}/feedback` | `X-Auth-Token` или демо | Оценка менеджера, `204` (этап 3) |
| GET | `/api/v1/stats` | `ADMIN_TOKEN` | Метрики за период, `?from=&to=` (этап 3) |
| POST | `/salesbot/amocrm/{secret}` | Секрет + JWT Salesbot | `widget_request` для автоотправки (этап 3) |

**`/health`:**
```json
{"status": "ok", "version": "0.1.0", "kb_version": "225b0436edc6", "llm_mode": "live",
 "llm_provider": "claude", "llm_model": "claude-opus-5-5",
 "llm_providers": {"claude": {"model": "claude-opus-5-5", "route": "anthropic", "available": true},
                   "kimi": {"model": "kimi-k3", "route": "gateway", "available": true}, "...": "..."},
 "amocrm": "mock", "queue": {"done": 12, "pending": 1}, "worker": "running"}
```
`status` становится `degraded`, а в ответе появляются `llm_problem` / `amocrm_problem`, если у модели по умолчанию нет доступа (нет ключа Claude API, не настроен шлюз) или amoCRM отклонил токен при старте. Остальные модели на статус не влияют. `llm_providers` есть только в режиме `live`. `available` означает, что доступ настроен и модель есть в списке моделей шлюза для ключа. Список запрашивается один раз при старте; `/health` сам шлюз не опрашивает. `queue` и `worker` есть, только когда интеграция с amoCRM включена.

**`POST /api/v1/suggest` — запрос:**
```json
{
  "message": "20 метров. Сколько с установкой и когда приедете? У нас ребёнок-аллергик, важно, чтобы было чисто",
  "history": [
    {"role": "client",  "text": "Добрый день, нужен кондиционер в спальню", "ts": "2026-09-29T10:00:00+03:00"},
    {"role": "manager", "text": "Здравствуйте, Анна! Какая площадь комнаты?", "ts": "2026-09-29T10:02:00+03:00", "author_name": "Ольга"}
  ],
  "replies": [],
  "lead": {"id": 1234, "pipeline": "Продажи", "stage": "Первичный контакт", "budget": null,
           "products": [], "tags": [], "contact_name": "Анна"},
  "channel": "telegram"
}
```

**Ответ `200`:**
```json
{
  "suggestion": { "...": "Suggestion, см. FUNCTIONALITY.md, раздел 10" },
  "meta": {
    "suggestion_id": "1e92feaf11f8481199135b16b3aba582",
    "mode": "full",
    "llm_mode": "live",
    "provider": "claude",
    "model": "claude-opus-5-5",
    "kb_version": "225b0436edc6",
    "latency_ms": 8400,
    "attempts": 1,
    "usage": {"input_tokens": 1450, "output_tokens": 1320,
              "cache_read_input_tokens": 4100, "cache_creation_input_tokens": 0},
    "warnings": [],
    "created_at": "2026-09-29T12:05:00Z"
  }
}
```

`meta.provider` и `meta.model` — модель, которая фактически ответила: при срабатывании запасной модели или серверного fallback Anthropic они отличаются от выбранных.

**Ошибки** возвращаются в едином формате `{"error": {"code": "...", "message": "...", "details"?: ...}}`:
- `401 unauthorized` — нет или неверный токен;
- `404 not_found`;
- `422 invalid_request` — некорректный запрос, в `details` — ошибки полей; неизвестная модель в `?provider=`;
- `422 kb_invalid` — БЗ не прошла проверку при перезагрузке;
- `502 llm_refused` — модель отказала и fallback не помог;
- `502 llm_bad_output` — ответ модели не соответствует схеме после повтора;
- `503 llm_unavailable` — сеть, лимиты, 5xx, нет учётных данных, шлюз не настроен, не принял ключ или не знает модель;
- `500 internal`.

## 8. Модель данных (SQLite)

Время хранится строками ISO 8601 в UTC с миллисекундами — такие строки сравниваются как время.

**`suggestions`** — результаты.
| Поле | Тип | Описание |
|---|---|---|
| `id` | TEXT PK | uuid |
| `dialog_id` | INTEGER NULL | Диалог amoCRM; NULL для вызовов REST и демо |
| `lead_id` | INTEGER NULL | Из `lead.id` запроса |
| `trigger_message_id` | TEXT NULL | Последнее сообщение клиента, на которое отвечали |
| `mode` | TEXT | `full` / `upsell_only` |
| `request_json` | TEXT (JSON) | Подготовленный запрос: обрезанная история, замаскированные ПДн |
| `payload_json` | TEXT (JSON) | `Suggestion` |
| `meta_json` | TEXT (JSON) | Модель, `usage`, задержка, версия БЗ, предупреждения |
| `note_id` | INTEGER NULL | id примечания в amoCRM |
| `status` | TEXT | `new` / `used` / `edited` / `rejected` (этап 3) |
| `final_text` | TEXT NULL | Что менеджер отправил на самом деле (этап 3) |
| `upsell_offered` | INTEGER NULL | Предложил ли менеджер допродажу (этап 3) |
| `created_at` | TEXT | |

**`dialogs`** — диалог = чат amoCRM.
| Поле | Тип | Описание |
|---|---|---|
| `id` | INTEGER PK | |
| `chat_key` | TEXT UNIQUE | `chat_id` чата, а если его нет — `talk-<id>` или `contact-<id>` |
| `talk_id` | TEXT NULL | id беседы |
| `contact_id` | INTEGER NULL | Контакт |
| `element_type`, `element_id` | INTEGER NULL | К чему привязан чат: 2 — сделка, 1 — контакт |
| `origin` | TEXT NULL | Канал: `telegram`, `whatsapp`, … |
| `contact_name` | TEXT NULL | Имя клиента из мессенджера (запасное, если в карточке пусто) |
| `created_at`, `updated_at` | TEXT | |

**`messages`** — история переписки из вебхуков.
| Поле | Тип | Описание |
|---|---|---|
| `amo_id` | TEXT PK | id сообщения amoCRM (ключ идемпотентности) |
| `dialog_id` | INTEGER FK | |
| `direction` | TEXT | `in` / `out` |
| `author_type` | TEXT | `contact` / `user` / `bot` |
| `author_name` | TEXT NULL | |
| `text` | TEXT | Может быть пустым, если пришло только вложение |
| `attachment_type` | TEXT NULL | `picture`, `voice`, `file`, … |
| `created_at` | TEXT | Время из amoCRM — по нему упорядочивается история |
| `received_at` | TEXT | Когда пришёл вебхук — по нему работает срок хранения |

**`jobs`** — очередь генераций.
| Поле | Тип | Описание |
|---|---|---|
| `id` | INTEGER PK | |
| `dialog_id` | INTEGER FK | Уникален среди `status='pending'` |
| `trigger_message_id` | TEXT | Последнее сообщение клиента |
| `run_at` | TEXT | Когда запускать (сдвигается паузой и повторами) |
| `status` | TEXT | `pending` / `running` / `done` / `stale` / `skipped` / `failed` |
| `attempts` | INTEGER | |
| `error` | TEXT NULL | Причина повтора, пропуска или сбоя |
| `suggestion_id` | TEXT NULL | Сгенерированная подсказка — чтобы повтор публикации не генерировал её заново |
| `created_at`, `updated_at` | TEXT | |

## 9. Методы API amoCRM

| Метод | Для чего |
|---|---|
| `GET /api/v4/account` | Проверка токена при старте (`/health`) |
| `GET /api/v4/leads/{id}?with=contacts,catalog_elements` | Данные сделки |
| `GET /api/v4/leads?filter[id][]=…&with=contacts,catalog_elements` | Сделки контакта, чтобы выбрать нужную |
| `GET /api/v4/contacts/{id}?with=leads` | Имя контакта и его сделки |
| `GET /api/v4/leads/pipelines` | Названия воронок и этапов (кэш на час) |
| `GET /api/v4/catalogs/{catalog_id}/elements?filter[id][]=…` | Названия товаров сделки (кэш) |
| `POST /api/v4/leads/{id}/notes` | Примечание с подсказкой (`service_message`) |
| `POST /api/v4/contacts/{id}/notes` | То же, если сделки нет |
| `POST /api/v4/webhooks` | Регистрация вебхука (`python -m app.amocrm.setup_webhook`) |
| `POST {return_url}` | Продолжение Salesbot с `data.reply` (этап 3) |

Авторизация — долгосрочный токен приватной интеграции. OAuth с обновлением токена не реализован: для приватной интеграции одного аккаунта он не нужен.

## 10. Форматы файлов базы знаний

id всех записей — латиница в kebab-case, уникальны во всей БЗ (на них ссылаются `kb_refs`). Лишние поля запрещены: опечатка в названии поля — ошибка, а не молча пропущенное значение.

**`products.yaml`**
```yaml
- id: ac-basic-09
  name: Сплит-система Basic 09
  category: Кондиционеры          # правила со скидкой на пару позиций берут товары той же категории
  price: 32900                     # ₽, целое число больше 0
  description: Кондиционер on/off для помещений до 25 м².
  for_whom: Спальня, гостиная или офис до 25 м².   # необязательно
  related: [install-standard, service-1y]           # необязательно; id товаров
- id: install-extra-route
  name: Дополнительный метр трассы
  category: Монтаж
  price: 1500
  unit: за метр                    # необязательно; если цена не за штуку
  description: Если трасса между блоками длиннее 3 м.
```

**`faq.yaml`**
```yaml
- id: faq-install-time
  questions: [когда приедете, сроки монтажа, как быстро установите]
  answer: Выезд мастера — в течение 1–3 рабочих дней после оплаты оборудования.
```

**`policies.yaml`**
```yaml
- id: pol-delivery
  title: Доставка
  text: При заказе с монтажом доставка бесплатная. Без монтажа — доставка 990 ₽.
```
Суммы с валютой в текстах FAQ, условий и правил (например, «990 ₽») проверка цен тоже считает допустимыми.

**`upsell_rules.yaml`**
```yaml
- id: up-service-with-install
  when: Клиент покупает кондиционер с монтажом или уточняет цену и сроки монтажа.
  offer: [service-1y]              # id из products.yaml
  argument: Фильтры и теплообменник нужно чистить раз в полгода…
  discount_percent: 20             # необязательно; тогда обязателен discount_condition
  discount_condition: при заказе вместе с монтажом
  not_when: Клиент жалуется, торгуется по цене основного товара или уже отказался.
```

**`company.md`** — свободный текст: кто мы, график, зона обслуживания.

**`tone_of_voice.md`** — свободный текст плюс обязательный раздел, который читает проверка стоп-фраз (поиск подстроки без учёта регистра):
```markdown
## Запрещённые фразы
- уважаемый клиент
- гарантируем 100%
- самая низкая цена
```

**`examples/amocrm/mock_account.json`** — «аккаунт» поддельного amoCRM: `account`, `pipelines` (со статусами), `catalogs` (с элементами), `contacts` (с id сделок), `leads` (воронка, этап, бюджет, контакты, теги, товары каталога, `updated_at`). id сделок совпадают со сценариями в `examples/scenarios/`.

## 11. Конфигурация

Все настройки задаются переменными окружения или файлом `.env` в корне проекта (шаблон — `.env.example`). Переменные окружения важнее `.env`. Относительные пути считаются от корня проекта.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` — без модели; `live` — настоящие модели |
| `LLM_PROVIDER` | `claude` | Модель по умолчанию: `claude` / `glm` / `deepseek` / `kimi` / `qwen` / `grok` — для amoCRM и запросов без выбора |
| `LLM_FALLBACK_PROVIDERS` | — | Запасные модели через запятую, по порядку: если модель по умолчанию не ответила |
| `ANTHROPIC_API_KEY` | — | Ключ Claude API (или профиль `ant auth login`). Если не задан, а шлюз настроен, Claude идёт через шлюз |
| `LLM_MODEL` | `claude-opus-5-5` | Модель Claude в Anthropic API |
| `LLM_EFFORT` | `medium` | Глубина рассуждений: `low` / `medium` / `high` / `xhigh` / `max`; для моделей шлюза приводится к их уровням |
| `LLM_MAX_TOKENS` | `8000` | Лимит вывода, включая рассуждения; при обрезке — один повтор с удвоенным лимитом |
| `LLM_TIMEOUT_SECONDS` | `60` | Таймаут запроса к LLM (SDK и клиент шлюза повторяют ещё до 2 раз) |
| `LLM_FALLBACKS` | `true` | Серверный fallback Anthropic API при отказе модели |
| `LIVE_DEMO_PASSWORD` | — | Живая модель за паролем на стенде в mock-режиме: без пароля — записанные ответы, с паролем на странице — живые модели (нужен шлюз или ключ Anthropic) |
| `LIVE_DEMO_DAILY_LIMIT` | `200` | Живых запросов в сутки на экземпляр сервиса (на Vercel экземпляров может быть несколько — это страховка; точный предел — квота ключа в шлюзе) |
| `LLM_GATEWAY_URL` | — | Адрес OpenAI-совместимого API шлюза, обычно `https://<шлюз>/v1`. Задаётся командой `python -m app.setup_gateway` |
| `LLM_GATEWAY_KEY` | — | Ключ шлюза |
| `LLM_GATEWAY_MODEL_CLAUDE` … `_GROK` | `claude-opus-5`, `glm-5.3`, `deepseek-v4-pro`, `kimi-k3`, `qwen3.8-max`, `grok-4.7` | id моделей в шлюзе (по умолчанию — как в New API) |
| `KB_DIR` | `knowledge_base` | Каталог БЗ |
| `HISTORY_LIMIT` | `20` | Сколько последних сообщений передавать модели |
| `PII_MASKING` | `true` | Маскировать ПДн перед LLM |
| `UPSELL_IN_REPLY` | `false` | Разрешить одну фразу допродажи прямо в ответе клиенту |
| `MOCK_LLM_DIR` | `examples/mock_llm` | Ответы для mock-режима LLM |
| `SCENARIOS_DIR` | `examples/scenarios` | Сценарии демо-страницы и имитатора |
| `API_TOKEN` | — | Если задан, `/api/v1/suggest*` требует `Authorization: Bearer` |
| `ADMIN_TOKEN` | — | Если задан, `/api/v1/kb*` требует `Authorization: Bearer`; пусто — открыто (локальная разработка) |
| `DB_PATH` | `data/app.db` (на Vercel — `/tmp/ai-manager/app.db`) | Файл SQLite |
| `LOG_LEVEL` | `INFO` | Уровень логов |
| `LOG_TEXTS` | `false` | Писать ли тексты сообщений и ответов в логи |
| `AMOCRM_MODE` | `off` | `off` / `mock` / `live`; в `.env.example` для демо — `mock` |
| `WEBHOOK_SECRET` | — | Секрет в пути вебхука; обязателен при `live`. В `mock` без него вебхук выключен, а страница «amoCRM (mock)» работает. В `.env.example` — `dev-secret`, только для демо |
| `AMOCRM_SUBDOMAIN` | — | Поддомен аккаунта (`live`); API — `https://<поддомен>.amocrm.ru` |
| `AMOCRM_BASE_URL` | — | Полный адрес API вместо поддомена (например, для kommo.com) |
| `AMOCRM_TOKEN` | — | Долгосрочный токен приватной интеграции (`live`); в `mock` можно не задавать |
| `AMOCRM_ACCOUNT_ID` | — | Если задан, вебхуки от других аккаунтов игнорируются |
| `AMOCRM_RPS` | `5` | Предел запросов в секунду к amoCRM (лимит amoCRM — 7) |
| `AMOCRM_TIMEOUT_SECONDS` | `15` | Таймаут запроса к amoCRM |
| `AMOCRM_MOCK_SEED` | `examples/amocrm/mock_account.json` | «Аккаунт» поддельного amoCRM |
| `NOTE_SERVICE_NAME` | `AI-помощник` | Подпись служебного примечания |
| `DEBOUNCE_SECONDS` | `6` | Пауза перед генерацией после последнего сообщения клиента |
| `ON_MANAGER_REPLIED` | `upsell_only` | `upsell_only` или `skip` |
| `WORKER_ENABLED` | `true` (на Vercel — `false`) | Запускать ли фоновый обработчик очереди |
| `WORKER_ON_REQUEST` | `false` (на Vercel — `true`) | Обрабатывать созревшие задачи при опросе ленты страницы «amoCRM (mock)» — там, где фоновый цикл не работает |
| `WORKER_CONCURRENCY` | `3` | Сколько задач обрабатывать одновременно |
| `WORKER_POLL_SECONDS` | `0.5` | Как часто проверять очередь |
| `JOB_MAX_ATTEMPTS` | `3` | Попыток на задачу |
| `JOB_RETRY_SECONDS` | `60` | Пауза перед повтором |
| `RETENTION_DAYS` | `30` | Срок хранения переписки, подсказок и завершённых задач |
| `AMOCRM_CLIENT_ID`, `AMOCRM_CLIENT_SECRET`, `AUTO_SEND` | —, —, `false` | Этап 3 |

## 12. Тесты и eval

```
tests/
├── conftest.py                    # фикстуры: демо-БЗ, копия БЗ для порчи, FakeLLM, make_suggestion, TestClient
├── test_kb_loader.py              # проверка файлов (8 видов ошибок), версия, фиксированный вывод, reload
├── test_money_and_pii.py          # извлечение сумм, маскирование ПДн без ложных срабатываний на ценах
├── test_prompts.py                # системный промпт стабилен, содержит всю БЗ; защита тегов; <replies>
├── test_guards.py                 # правила сумм, ссылки, товары, жалоба, стоп-фразы, upsell_only
├── test_assistant.py              # meta, ПДн, лимит истории, повторы, ошибки
├── test_llm_client.py             # параметры запроса к SDK, разбор ответа, ошибки SDK, нет ключа
├── test_gateway_llm.py            # клиент шлюза на поддельном HTTP: запрос по моделям, разбор, упрощение, ошибки
├── test_llm_registry.py           # реестр, маршруты, запасные модели, ?provider=, /api/v1/llm/providers, выбор на странице
├── test_setup_gateway.py          # команда подключения шлюза: .env, проверка ключа, подсказки по моделям
├── test_record_scenarios.py       # запись ответов на сценарии: запрос как на странице, отбор по проверкам
├── test_live_demo.py              # живая модель за паролем: вход, выбор ассистента, подбор пароля, лимит, страница
├── test_mock_llm_and_scenarios.py # ответы mock проходят проверки, сценарии end-to-end, FAQ, лёгкие импорты
├── test_api.py                    # эндпоинты, токены, коды ошибок, сохранение, перезагрузка БЗ
├── test_cli.py                    # текстовый и JSON-вывод, stdin, коды возврата, --provider
├── test_eval_runner.py            # кейсы валидны, проверки eval, отчёт в mock-режиме, сравнение моделей
├── test_amocrm_webhooks.py        # разбор вебхуков по примерам документации: форма, JSON, вложения, порядок
├── test_amocrm_client.py          # клиент против поддельного сервера: чтение, кэш, примечания, повторы, лимит; контекст
├── test_worker.py                 # очередь: пауза, дубли, режимы, stale, вложения, повторы, сбои, перезапуск, очистка
├── test_webhook_api.py            # вебхук по HTTP, health, настройки; API и страница «amoCRM (mock)»
├── test_amocrm_tools.py           # имитатор end-to-end через TestClient, регистрация вебхука
└── test_amocrm_connect.py         # подключение аккаунта: разбор адреса, проверка токена, запись в .env
evals/
├── cases.yaml                     # 31 кейс: вход + ожидаемые свойства результата
├── run_eval.py                    # прогон, проверки, метрики, стоимость, отчёт, --provider (all — сравнение), --record
├── record_scenarios.py            # ответы модели на демо-сценарии → examples/mock_llm (только прошедшие проверки)
└── reports/                       # отчёты прогонов в Markdown
```

Пример кейса eval:
```yaml
- id: complaint-leak
  tags: [complaint]
  message: Из внутреннего блока капает вода прямо на пол! Вчера только поставили.
  lead: {stage: Успешно реализовано, products: [ac-basic-09, install-standard]}
  expect:
    sentiment: negative
    upsell_timing: [not_now]
    kb_refs_any: [pol-complaints, pol-warranty]
```

Все проверки eval перечислены в шапке `evals/cases.yaml`. Для каждого кейса дополнительно проверяется, что нет предупреждений о суммах вне БЗ; исключения задаются в `allow_warnings`.
