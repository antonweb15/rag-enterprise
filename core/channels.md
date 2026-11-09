# Channels & End-User Delivery Specification (`core/channels.md`)

> **Document status**: this is the **single source of truth** for the user (end, search) interface: Web Widget, Telegram, headless REST/SSE, CLI, the `BaseChannel` abstraction, branding and the Cloud/Box deployment modes. Other core files (`architecture.md`, `licensing_and_distribution.md`, `modules_spec.md`, `storage_schema.md`) only provide **links** here — duplicating content is forbidden, to avoid breeding desync.
>
> The operator (admin) contour is out of this document, it is described in [`admin_panel.md`](./admin_panel.md). `AdminConfig.branding` (admin-panel branding) does not belong here.

---

## 1. Purpose and boundaries

- **Purpose**: a single, channel-neutral contract for delivering RAG answers to the end user and receiving their requests.
- **Principle**: "one codebase, two modes" (Cloud/Box) via the runtime config, without forks.
- **Boundaries (what we do NOT build now)**: Slack / MS Teams / WhatsApp — only behind a license feature flag, **not implemented in advance**. Each such channel = its own format (blocks/templates) and a real cost with a dubious ROI at the start of enterprise RAG.

---

## 2. Channel registry (`ChannelType`)

The canonical enum is in [`modules_spec.md`](./modules_spec.md) (`ChannelType`). Channels are **license feature flags** (`features: ["web_widget", "telegram", "rest_api", ...]`), not "hardwired".

| `ChannelType` | Value | Audience / purpose | Priority |
|---|---|---|---|
| `WEB_WIDGET` | `web` | End user on the client's site (white-label, Shadow DOM). The **primary** interface. One for Cloud and Box | primary |
| `TELEGRAM` | `telegram` | End user in Telegram. A secondary delivery channel (not admin) | secondary |
| `REST_SSE` | `rest_sse` | The client's custom integrations (headless `/api/v1/chat/stream`) | on request |
| `CLI` | `cli` | Air-gapped Box (`typer` console) | Box |

> The legacy value `'fastapi'` in the DB (`users.channel`, `conversations.channel`) is migrated to `'rest_sse'` (backfill — see [`storage_schema.md`](./storage_schema.md) §2.1).

---

## 3. Channel capabilities (`ChannelCapabilities`)

The model is in [`modules_spec.md`](./modules_spec.md) (`ChannelCapabilities`). **The key rule**: the flags are read **only at the channel boundary** (render/delivery). **The search business logic (retrieval, steps 4-9 of the Query Flow) does NOT branch on capabilities.** Each adapter must have a **contract test** confirming the declared flags, otherwise "the channel lies".

The canonical capabilities table:

| capability | `web` | `telegram` | `rest_sse` | `cli` |
|---|---|---|---|---|
| `supports_streaming` | ✅ | ✅ | ✅ | ✅ |
| `supports_markdown` | ✅ | ✅ | ❌ (raw text/JSON) | ⚠️ (plain) |
| `supports_citations` | ✅ | ✅ | ✅ (in payload) | ✅ |
| `supports_feedback_buttons` | ✅ | ✅ | ❌ | ❌ |
| `supports_attachments` | ⚠️ | ✅ | ❌ | ❌ |
| `supports_reply_threading` | ❌ | ✅ | ❌ | ❌ |
| `supports_commands` | ❌ | ✅ (`/start` etc.) | ❌ | ✅ |
| `supports_callback_payloads` | ✅ | ✅ | ❌ | ❌ |
| `supports_cancellation` | ✅ | ⚠️ | ✅ | ✅ |
| `max_message_length` | `None` | `4096` (Telegram limit) | `None` | `None` |
| `max_attachments_per_message` | `0`* | `>0`* | `0` | `0` |

> *`max_attachments_per_message` in the model defaults to `0` (conservatively). Adapters that actually support attachments (`supports_attachments=✅`, e.g. Telegram) **must** set `max_attachments_per_message > 0` at runtime; for `web` the value depends on the widget configuration.

> The model defaults are **conservative** (everything `False`, `max_message_length=None`): an absent/unknown capability does not break the render. `max_message_length=4096` is the Telegram limit, NOT a "neutral" default; web/REST/CLI are unlimited.

---

## 4. Channel-neutral input (`ChannelEvent`) and the "one input, two outputs" invariant

The models are in [`modules_spec.md`](./modules_spec.md) (`ChannelEvent`, `ChannelEventType`, `AttachmentRef`, `BaseChannel`).

- **Input**: `BaseChannel.receive()` returns a stream of normalized `ChannelEvent`s. Each adapter **must** map native events (a Telegram message, an SSE request, a CLI command) into a `ChannelEvent`.
- **Core**: the retrieval pipeline accepts **only** a `ChannelEvent` and knows nothing about the specific channel.
- **Output**: `BaseChannel.send_response()` / `stream_response()`.
- `request_cancellation()` — an **optional** hook: the core calls it but is not obliged to rely on it; adapters without `supports_cancellation` may leave it as a no-op.

The full Query Flow (channel-neutral) — see [`architecture.md`](./architecture.md) §2.

---

## 5. Web widget: embed, handshake, Cloud/Box

### 5.1. Embedding (`<script>`)

```html
<!-- Cloud: one-line integration on the client's site -->
<script
  src="https://cloud.your-rag-domain.com/widget/v1.js"
  data-tenant-slug="company-slug"
  data-theme="dark"
  defer>
</script>
```

- The widget is rendered inside a **Shadow DOM** (Web Component), isolating styles/scripts from the client's page.
- The embed script is **versioned** (`/widget/v1.js`); incompatible changes — a new major path version.
- An alternative for the strictest contours is the **iframe mode** instead of Shadow DOM.

### 5.2. `data-api-url` — strictly mode-dependent behavior

For the box the same script must be able to specify where to pull the API from:

```html
<!-- Box: the widget on the client's site in a closed contour -->
<script
  src="https://kb.client-domain.ru/widget/v1.js"
  data-tenant-slug="company-slug"
  data-api-url="https://kb.client-domain.ru"
  defer>
</script>
```

**The behavior depends on `deployment_mode`:**
- `saas_cloud`: `data-api-url` is **ignored** (the API is hard-pinned to the CDN/cloud domain). The passed value is logged as suspicious (anti-phishing/anti-traffic-hijacking).
- `on_premise_box`: `data-api-url` is **applied**, but the request still passes the Origin allowlist.

> **Acceptance (mandatory negative test)**: in `cloud` mode the passed `data-api-url` is ignored and logged; in `box` — applied and still checked against `allowed_origins`. Without the test the behavior regresses.

### 5.3. Two-step Session Handshake

The contract is `WebWidgetSessionRequest` / `WebWidgetSessionResponse` in [`modules_spec.md`](./modules_spec.md). **Two modes** (`auth_mode`) are supported: `anonymous` (default) and `oidc` (authenticated, behind the `sso_oidc` license flag).

1. **Session initialization**:
   - The widget performs `POST /api/v1/widget/session` with the body `{ "slug": "company-slug", "auth_mode": "anonymous" | "oidc", "id_token": <optional> }`.
   - The backend checks the `Origin` header against the strict allowlist `tenants.allowed_origins` (**without wildcard `*`**; the DB default is `'[]'`, see [`storage_schema.md`](./storage_schema.md)).
   - **`anonymous`**: in the `users` table an anonymous user `channel = 'web'`, `access_level = 0 (Public)` is created/found. Confidential documents are fundamentally unavailable in the widget.
   - **`oidc`** (optional, Stage C): the widget passes an `id_token` (OIDC) / SAML assertion; the backend **verifies the signature** against the IdP (`oidc_issuer`/`oidc_jwks_url`, see `Settings.oidc_*` in [`modules_spec.md`](./modules_spec.md)), extracts the `subject` and groups (`oidc_groups_claim`), finds/creates `users` with `external_subject` and maps the IdP groups to `groups` (`external_ref`). The resulting `access_level`/`group_ids` are **recomputed on the backend** from the DB/verified claim — **never** taken from the client request body.
   - The backend returns a short-lived signed session JWT (TTL = 1 hour), bound to `tenant_id` and `session_id` (for `oidc` — also `subject + groups` inside the JWT).
2. **Streaming chat (SSE)**:
   - `POST /api/v1/chat/stream` accepts `Authorization: Bearer <session_jwt>`.
   - A hard per-tenant Rate Limiting at the middleware level (by IP and session token).

> **Box**: the IdP is the client's local directory (`data-api-url` points to the box; OIDC/SCIM or a CSV group import for air-gapped). **Cloud**: multi-tenant OIDC, `oidc_*` configured per-tenant. The "authorization never on the client" invariant also extends to `access_level`/claims.

### 5.4. Hardening (mandatory)

- Origin allowlist **without wildcard**; the default is empty.
- A short-lived JWT; **no licensing/quota logic on the client** (an invariant, see [`licensing_and_distribution.md`](./licensing_and_distribution.md) §4.5).
- CSP on the widget page.
- Separate worker pools and rate limits: the public `widget` vs the private `admin` (see [`admin_panel.md`](./admin_panel.md) §10, [`eval_and_ops.md`](./eval_and_ops.md)).

### 5.5. Cloud vs Box — only through config

| Aspect | `saas_cloud` | `on_premise_box` |
|---|---|---|
| Serving `widget/v1.js` | CDN / Nginx | the box itself (`StaticFiles` / the client's reverse proxy) |
| `data-api-url` override | ignored | allowed (+ Origin allowlist) |
| Widget behavior and code | identical | identical |

---

## 6. Telegram: white-label and limitations

- **The bot avatar/photo** — **only via @BotFather**. The Bot API cannot do it (`setMyPhoto` does not exist). Setting the avatar from the admin panel is **technically impossible** — this is recorded as a limitation.
- **Configurable from the admin panel via the Bot API**: name (`setMyName`), description (`setMyDescription`), short description (`setMyShortDescription`), commands (`setMyCommands`), the greeting, the default rights (`setMyDefaultAdministratorRights`).
- **Per-tenant identity is expensive**: one token = one bot = a shared identity. "A separate bot for each client" requires a separate `telegram_bot_token` per tenant (multiplying webhooks/polling, token management, Telegram rate limits) — this is a **paid option**, not the default. By default — one shared bot.
- The model is `TelegramBranding` in [`modules_spec.md`](./modules_spec.md).

---

## 7. Typed branding

The models are `BrandingConfig` / `WidgetBranding` / `TelegramBranding` in [`modules_spec.md`](./modules_spec.md). Stored in `tenants.config` JSONB (see [`storage_schema.md`](./storage_schema.md)).

### 7.1. White-label completeness by channel
- **Web widget** — 100% controlled from the admin panel: title, subtitle, logo, color, greeting, hints, disclaimer.
- **Telegram** — partially: name/description/commands/greeting from the admin panel; the avatar — by hand in BotFather.

### 7.2. Versioning and migration
- `config_version` in each model; the JSONB content evolves **additive-only**.
- When reading old tenants, missing fields are filled with defaults; incompatible changes raise `config_version` with an explicit migration rule.

### 7.3. Sanitization (white-label security)

The branding fields are rendered on a **foreign** site, so pattern validation is **not enough**:
- `welcome_message` / `disclaimer` / `subtitle` / `title` — rendered as **text, not raw HTML** (escaping; HTML/scripts forbidden → XSS protection).
- `logo_url` — only `https://` (pattern) **and** a backend check against an allowlist of Content-Type/extensions (images only); the scheme by itself does not protect against active content.
- `primary_color` — strictly `^#[0-9a-fA-F]{6}$`.
- Limits against DoS/UI breakage: `welcome_message ≤ 1000`, `disclaimer ≤ 500`, `suggested_questions ≤ 5` (each ≤ 120 chars), `commands` limited by count and length.
- A **CSP** is mandatory on the widget page.

### 7.4. Configuration delivery
- Public widget: `GET /api/v1/widget/config` (no secrets, only `WidgetBranding` + capabilities).
- Admin panel: `GET /api/v1/admin/config`.

---

## 8. Strengths preserved unchanged

- Separate pools `widget` (public) vs `admin` (private) and separate rate limits.
- "One codebase, two modes" via config (do not reinvent).
- Forbidding any licensing/quota logic on the client.
