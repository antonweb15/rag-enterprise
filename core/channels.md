# Channels & End-User Delivery Specification (`core/channels.md`)

> **Статус документа**: это **единственный источник истины** по пользовательскому (конечному, поисковому) интерфейсу: Web Widget, Telegram, headless REST/SSE, CLI, абстракция `BaseChannel`, брендинг и режимы развёртывания Cloud/Box. Другие файлы ядра (`architecture.md`, `licensing_and_distribution.md`, `modules_spec.md`, `storage_schema.md`) дают только **ссылки** сюда — дублировать содержимое запрещено, чтобы не плодить рассинхрон.
>
> Операторский (админский) контур — вне этого документа, он описан в [`admin_panel.md`](./admin_panel.md). `AdminConfig.branding` (брендинг админки) сюда не относится.

---

## 1. Цель и границы

- **Цель**: единый, канало-нейтральный контракт доставки ответов RAG конечному пользователю и приёма его запросов.
- **Принцип**: «один код, два режима» (Cloud/Box) через runtime-конфиг, без форков.
- **Границы (что НЕ строим сейчас)**: Slack / MS Teams / WhatsApp — только за feature-флагом лицензии, **не реализуются заранее**. Каждый такой канал = свой формат (blocks/templates) и реальная стоимость при сомнительном ROI на старте enterprise-RAG.

---

## 2. Реестр каналов (`ChannelType`)

Канонический enum — в [`modules_spec.md`](./modules_spec.md) (`ChannelType`). Каналы являются **feature-флагами лицензии** (`features: ["web_widget", "telegram", "rest_api", ...]`), а не «зашитыми».

| `ChannelType` | Значение | Аудитория / назначение | Приоритет |
|---|---|---|---|
| `WEB_WIDGET` | `web` | Конечный пользователь на сайте клиента (white-label, Shadow DOM). **Основной** интерфейс. Один для Cloud и Box | основной |
| `TELEGRAM` | `telegram` | Конечный пользователь в Telegram. Вторичный канал доставки (не админский) | вторичный |
| `REST_SSE` | `rest_sse` | Кастомные интеграции клиента (headless `/api/v1/chat/stream`) | по запросу |
| `CLI` | `cli` | Air-gapped Box (`typer`-консоль) | Box |

> Legacy-значение `'fastapi'` в БД (`users.channel`, `conversations.channel`) мигрируется в `'rest_sse'` (backfill — см. [`storage_schema.md`](./storage_schema.md) §2.1).

---

## 3. Возможности каналов (`ChannelCapabilities`)

Модель — в [`modules_spec.md`](./modules_spec.md) (`ChannelCapabilities`). **Ключевое правило**: флаги читаются **только на границе канала** (рендер/доставка). **Бизнес-логика поиска (retrieval, шаги 4–9 Query Flow) по capabilities НЕ ветвится.** Каждый адаптер обязан иметь **контракт-тест**, подтверждающий заявленные флаги, иначе «канал врёт».

Каноническая таблица возможностей:

| capability | `web` | `telegram` | `rest_sse` | `cli` |
|---|---|---|---|---|
| `supports_streaming` | ✅ | ✅ | ✅ | ✅ |
| `supports_markdown` | ✅ | ✅ | ❌ (сырой текст/JSON) | ⚠️ (plain) |
| `supports_citations` | ✅ | ✅ | ✅ (в payload) | ✅ |
| `supports_feedback_buttons` | ✅ | ✅ | ❌ | ❌ |
| `supports_attachments` | ⚠️ | ✅ | ❌ | ❌ |
| `supports_reply_threading` | ❌ | ✅ | ❌ | ❌ |
| `supports_commands` | ❌ | ✅ (`/start` и пр.) | ❌ | ✅ |
| `supports_callback_payloads` | ✅ | ✅ | ❌ | ❌ |
| `supports_cancellation` | ✅ | ⚠️ | ✅ | ✅ |
| `max_message_length` | `None` | `4096` (лимит Telegram) | `None` | `None` |
| `max_attachments_per_message` | `0`* | `>0`* | `0` | `0` |

> *`max_attachments_per_message` в модели по умолчанию `0` (консервативно). Адаптеры, реально поддерживающие вложения (`supports_attachments=✅`, напр. Telegram), **обязаны** выставить `max_attachments_per_message > 0` в рантайме; для `web` значение зависит от конфигурации виджета.

> Дефолты в модели **консервативные** (всё `False`, `max_message_length=None`): отсутствие/неизвестность возможности не ломает рендер. `max_message_length=4096` — это лимит Telegram, а НЕ «нейтральный» дефолт; web/REST/CLI не ограничены.

---

## 4. Канало-нейтральный вход (`ChannelEvent`) и инвариант «один вход, два выхода»

Модели — в [`modules_spec.md`](./modules_spec.md) (`ChannelEvent`, `ChannelEventType`, `AttachmentRef`, `BaseChannel`).

- **Вход**: `BaseChannel.receive()` возвращает поток нормализованных `ChannelEvent`. Каждый адаптер **обязан** мапить нативные события (сообщение Telegram, SSE-запрос, CLI-команду) в `ChannelEvent`.
- **Ядро**: retrieval-конвейер принимает **только** `ChannelEvent` и ничего не знает о конкретном канале.
- **Выход**: `BaseChannel.send_response()` / `stream_response()`.
- `request_cancellation()` — **опциональный** hook: ядро его вызывает, но не обязано полагаться на него; адаптеры без `supports_cancellation` могут оставить no-op.

Полный Query Flow (канало-нейтральный) — см. [`architecture.md`](./architecture.md) §2.

---

## 5. Web-виджет: embed, handshake, Cloud/Box

### 5.1. Встраивание (`<script>`)

```html
<!-- Cloud: подключение одной строкой на сайте клиента -->
<script
  src="https://cloud.your-rag-domain.com/widget/v1.js"
  data-tenant-slug="company-slug"
  data-theme="dark"
  defer>
</script>
```

- Виджет рендерится внутри **Shadow DOM** (Web Component), изолируя стили/скрипты от страницы клиента.
- Эмбед-скрипт **версионируется** (`/widget/v1.js`); несовместимые изменения — новая мажорная версия пути.
- Альтернатива для самых строгих контуров — **iframe-режим** вместо Shadow DOM.

### 5.2. `data-api-url` — строго режим-зависимое поведение

Для коробки один и тот же скрипт должен уметь указывать, откуда тянуть API:

```html
<!-- Box: виджет на сайте клиента в закрытом контуре -->
<script
  src="https://kb.client-domain.ru/widget/v1.js"
  data-tenant-slug="company-slug"
  data-api-url="https://kb.client-domain.ru"
  defer>
</script>
```

**Поведение зависит от `deployment_mode`:**
- `saas_cloud`: `data-api-url` **игнорируется** (API жёстко прибит к CDN/облачному домену). Переданное значение логируется как подозрительное (анти-фишинг/анти-угон трафика).
- `on_premise_box`: `data-api-url` **применяется**, но запрос всё равно проходит Origin-allowlist.

> **Acceptance (обязательный негативный тест)**: в режиме `cloud` переданный `data-api-url` игнорируется и логируется; в `box` — применяется и всё равно проверяется против `allowed_origins`. Без теста поведение регрессирует.

### 5.3. Двухэтапный Session Handshake

Контракт — `WebWidgetSessionRequest` / `WebWidgetSessionResponse` в [`modules_spec.md`](./modules_spec.md).

1. **Инициализация сессии**:
   - Виджет выполняет `POST /api/v1/widget/session` с телом `{ "slug": "company-slug" }`.
   - Бэкенд проверяет заголовок `Origin` против строгого allowlist `tenants.allowed_origins` (**без wildcard `*`**; дефолт в БД — `'[]'`, см. [`storage_schema.md`](./storage_schema.md)).
   - В таблице `users` создаётся/находится анонимный пользователь `channel = 'web'`, `access_level = 0 (Public)`.
   - Бэкенд возвращает короткоживущий подписанный session JWT (TTL = 1 час), привязанный к `tenant_id` и `session_id`.
2. **Потоковый чат (SSE)**:
   - `POST /api/v1/chat/stream` принимает `Authorization: Bearer <session_jwt>`.
   - Жёсткий per-tenant Rate Limiting на уровне middleware (по IP и session-токену).

### 5.4. Hardening (обязательно)

- Origin allowlist **без wildcard**; дефолт пустой.
- Короткоживущий JWT; **никакой лицензионной/квотной логики на клиенте** (инвариант, см. [`licensing_and_distribution.md`](./licensing_and_distribution.md) §4.5).
- CSP на странице виджета.
- Раздельные воркер-пулы и rate-limit: публичный `widget` vs приватный `admin` (см. [`admin_panel.md`](./admin_panel.md) §10, [`eval_and_ops.md`](./eval_and_ops.md)).

### 5.5. Cloud vs Box — только через конфиг

| Аспект | `saas_cloud` | `on_premise_box` |
|---|---|---|
| Отдача `widget/v1.js` | CDN / Nginx | сама коробка (`StaticFiles` / reverse-proxy клиента) |
| `data-api-url` override | игнорируется | разрешён (+ Origin-allowlist) |
| Поведение и код виджета | идентичны | идентичны |

---

## 6. Telegram: white-label и ограничения

- **Аватар/фото бота** — **только через @BotFather**. Bot API это не умеет (`setMyPhoto` не существует). Из админки аватар настроить **технически нельзя** — фиксируется как ограничение.
- **Настраивается из админки через Bot API**: имя (`setMyName`), описание (`setMyDescription`), короткое описание (`setMyShortDescription`), команды (`setMyCommands`), приветствие, права по умолчанию (`setMyDefaultAdministratorRights`).
- **Per-tenant идентичность дорогая**: один токен = один бот = общая идентичность. «Свой бот у каждого клиента» требует отдельного `telegram_bot_token` на тенанта (умножение вебхуков/polling, менеджмент токенов, rate-limit Telegram) — это **платная опция**, не дефолт. По умолчанию — один общий бот.
- Модель — `TelegramBranding` в [`modules_spec.md`](./modules_spec.md).

---

## 7. Типизированный брендинг

Модели — `BrandingConfig` / `WidgetBranding` / `TelegramBranding` в [`modules_spec.md`](./modules_spec.md). Хранятся в `tenants.config` JSONB (см. [`storage_schema.md`](./storage_schema.md)).

### 7.1. Полнота white-label по каналам
- **Web-виджет** — 100% управляется из админки: заголовок, подзаголовок, логотип, цвет, приветствие, подсказки, дисклеймер.
- **Telegram** — частично: имя/описание/команды/приветствие из админки; аватар — руками в BotFather.

### 7.2. Версионирование и миграция
- `config_version` в каждой модели; JSONB-контент эволюционирует **additive-only**.
- При чтении старых тенантов отсутствующие поля заполняются дефолтами; несовместимые изменения повышают `config_version` с явным правилом миграции.

### 7.3. Санитизация (безопасность white-label)

Поля брендинга рендерятся на **чужом** сайте, поэтому pattern-валидации **недостаточно**:
- `welcome_message` / `disclaimer` / `subtitle` / `title` — рендерятся как **текст, не raw-HTML** (экранирование; запрет HTML/скриптов → защита от XSS).
- `logo_url` — только `https://` (pattern) **и** проверка на бэкенде по allowlist Content-Type/расширений (только изображения); схема сама по себе от активного контента не защищает.
- `primary_color` — строго `^#[0-9a-fA-F]{6}$`.
- Лимиты против DoS/UI-поломки: `welcome_message ≤ 1000`, `disclaimer ≤ 500`, `suggested_questions ≤ 5` (каждый ≤ 120 символов), `commands` ограничены по количеству и длине.
- На странице виджета обязателен **CSP**.

### 7.4. Выдача конфигурации
- Публичный виджет: `GET /api/v1/widget/config` (без секретов, только `WidgetBranding` + capabilities).
- Админка: `GET /api/v1/admin/config`.

---

## 8. Сильные стороны, сохраняемые без изменений

- Раздельные пулы `widget` (публичный) vs `admin` (приватный) и раздельные rate-limit.
- «Один код, два режима» через конфиг (не переизобретать).
- Запрет любой лицензионной/квотной логики на клиенте.
