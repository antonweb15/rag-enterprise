# License Server (Issuance / Revocation / Heartbeat) Specification (`core/license_server.md`)

## 1. Цель и границы подсистемы

**License Server** — **серверная половина модели лицензирования** вендора: выпуск (issuance), отзыв (revocation), приём heartbeat и публикация списка отзыва (CRL). Ранее в спеках присутствовала только клиентская валидация (`BaseLicenseValidator`) и URL мастер-сервера (`license_server_url`) — сам сервер отсутствовал (зафиксировано в `deepseek-criticism.md` §8). Этот документ достраивает недостающую половину.

**Разграничение с существующим:**

| Сторона | Где описано | Что делает |
|---|---|---|
| **Клиент (валидатор)** | `modules_spec.md` (`BaseLicenseValidator`), `admin_panel.md` §8 | Offline-проверка подписи Ed25519, срока, `hardware_id`; self-service активация по коду/`.key`/токену; heartbeat-пинг |
| **Сервер (issuer)** | **этот документ**, `modules_spec.md` (`BaseLicenseServer`) | Выпуск/отзыв/продление, приём heartbeat, CRL — **только контур вендора** |

**Вне границ**: License Server **не** входит в артефакт `on_premise_box`. Доступ к выпуску — только через Vendor Backoffice (см. [`vendor_backoffice.md`](./vendor_backoffice.md)).

---

## 2. Принципы (зафиксированные решения)

1. **Единственный источник подписи**: лицензии подписываются **только** offline Ed25519-ключом вендора. Приватный ключ живёт в отдельном **signing-сервисе / HSM / KMS** (`license_signing_service_url`), а **не** в веб-приложении или БД backoffice.
2. **Разделение подписи и сессий**: `LICENSE_SIGNING_KEY` **никогда** не переиспользуется для подписи сессий (инвариант уже зафиксирован в `admin_panel.md` §4.3).
3. **Два профиля лицензии**:
   - `online_subscription` (Cloud/подписка): есть `expires_at`, периодический heartbeat, онлайн-проверка отзыва.
   - `airgap_enterprise` (Box/офлайн): привязка к `hardware_id`, допускается бессрочность (`expires_at = NULL`), проверка отзыва — по CRL-снапшоту при доступности сети.
4. **Grace Period**: при недоступности сервера/heartbeat инсталляция работает в grace-режиме (по умолчанию 72ч, согласовано с `BaseLicenseValidator.verify_heartbeat`), после чего — **Restricted Mode** с текстом от бэкенда.
5. **Revocation List (CRL)**: отзыв публикуется в CRL (`license_crl_url`); клиент при онлайне сверяет лицензию с CRL. Офлайн — по последнему доступному снапшоту.
6. **Ротация ключей**: поддержка нескольких активных публичных ключей (key id в токене) для бесшовной ротации без массовой переактивации.
7. **Полный аудит**: каждая операция issuance/revocation/extend пишется в `backoffice_audit_log` (см. [`storage_schema.md`](./storage_schema.md) §2.2).

---

## 3. Серверный контракт (`BaseLicenseServer`)

Контракт определён в [`modules_spec.md`](./modules_spec.md):

- `issue_license(LicenseIssueRequest) -> license_token` — выпуск подписанного Ed25519-токена (подпись делегируется signing-сервису).
- `revoke_license(LicenseRevokeRequest)` — отметка `revoked_at`/`revocation_reason` + публикация в CRL.
- `register_heartbeat(hardware_id, license_id)` — обновление `last_heartbeat_at`, расчёт grace/restricted.
- `generate_activation_code(customer_id, plan) -> ActivationCode` — генерация кода `RAG-ENT-XXXX-YYYY-ZZZZ`.

---

## 4. Контракты эндпоинтов

### 4.1. Выпуск/управление (приватно, только Vendor Backoffice)

| Метод | Путь | Запрос | Ответ | Назначение |
|---|---|---|---|---|
| `POST` | `/api/v1/backoffice/licenses/issue` | `LicenseIssueRequest` | `{license_token, license_id}` | Выпуск подписанного токена |
| `POST` | `/api/v1/backoffice/licenses/revoke` | `LicenseRevokeRequest` | `204` | Отзыв + публикация в CRL |
| `POST` | `/api/v1/backoffice/licenses/{id}/extend` | `{expires_at}` | `LicenseStatus` | Продление |
| `GET` | `/api/v1/backoffice/licenses/{id}/key-file` | — | `application/octet-stream` | Скачивание `.key` (airgap, привязка `hardware_id`) |

### 4.2. Клиентский обмен (публичный мастер-сервер лицензий)

| Метод | Путь | Запрос | Ответ | Назначение |
|---|---|---|---|---|
| `POST` | `/licenses/heartbeat` | `HeartbeatPayload` (`license_id`, `hardware_id`, `version`, обезличенные агрегаты) | `{status, grace_until, restricted}` | Периодический heartbeat от инсталляции |
| `GET` | `/licenses/crl` | `?since=` | `{revoked: [...], generated_at}` | Список отзыва (CRL-снапшот) |
| `POST` | `/licenses/reactivate` | `{license_id, old_hardware_id, new_hardware_id, reason}` | `LicenseStatus` | Переактивация при смене железа (enterprise-кейс) |

**Общие правила:**
- Приватные эндпоинты (§4.1) требуют backoffice-роль с правом выдачи (`superadmin`/`finance`) + 2FA + CSRF.
- Публичные эндпoint-ы (§4.2) аутентифицируются по лицензии/`hardware_id`; rate-limit — публичная политика.
- Heartbeat принимает только **обезличенные агрегаты** (`HeartbeatPayload`, см. [`modules_spec.md`](./modules_spec.md)); при `telemetry_opt_out: true` поля метрик игнорируются. Трейсы/документы/ПД через heartbeat не передаются.

---

## 5. Hardware fingerprint и переактивация

- Текущий `hardware_id = sha256(cpu_id + mac)` **ненадёжен** в Docker/K8s/VM (отмечено в `deepseek-criticism.md` §11–13). Рекомендуется составной fingerprint с несколькими сигналами и порогом совпадения, а также явный путь **переактивации** (`/licenses/reactivate`) с аудитом и лимитом смен.
- Для `airgap_enterprise` `.key`-файл выпускается с привязкой к `hardware_id`; переактивация на новом железе — через вендора (из backoffice или customer portal будущей волны).

---

## 6. Безопасность

- Приватный ключ подписи — только в signing-сервисе / HSM / KMS; backoffice-приложение вызывает подпись по защищённому каналу и **не** хранит ключ.
- Публичные ключи (с key id) распространяются в артефактах для offline-валидации; ротация — добавлением нового key id без отзыва старых лицензий.
- CRL подписывается тем же офлайн-ключом; клиент проверяет подпись CRL перед применением.
- Все операции issuance/revocation/reactivation — с полным аудитом (`backoffice_audit_log`).

---

## 7. Приоритизация

- **MVP (волна 1)**: `issue`/`revoke`, CRL, `heartbeat`, расчёт grace/restricted; интеграция signing-сервиса (или KMS) без собственного HSM.
- **Волна 2**: `extend`/`upgrade`, скачивание `.key`, ротация ключей (key id).
- **Волна 3**: устойчивый fingerprint + самостоятельная переактивация, дистрибуция образов и временные токены доступа к приватному registry.
