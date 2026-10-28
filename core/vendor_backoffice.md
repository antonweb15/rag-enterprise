# Vendor Backoffice (Superadmin) Specification (`core/vendor_backoffice.md`)

## 1. Цель и границы подсистемы

**Vendor Backoffice** — отдельный **суперадминский контур вендора**, через который сотрудники вендора управляют клиентами, лицензиями, активационными кодами, реестром коробок и кросс-тенантной поддержкой. Это **не** тенантная админка (`admin_panel.md`): у них разные аудитории, данные, роли и безопасность.

**Три контура системы (разграничение):**

| Контур | Аудитория | Доступ | Назначение |
|---|---|---|---|
| **Vendor Backoffice** (`/api/v1/backoffice/*`) | Сотрудники вендора | Приватный, только вендор | Клиенты, выдача лицензий, коды, коробки, кросс-тенантная поддержка |
| **Tenant Admin SPA** (`/api/v1/admin/*`) | Операторы/владельцы тенанта | Приватный, после аутентификации | Документы, лицензии (self-service), мониторинг (см. `admin_panel.md`) |
| **Web Widget / каналы** (`/api/v1/widget/*`, `BaseChannel`) | Конечные пользователи | Публичный | Чат-ответы по базе знаний (см. `channels.md`) |

**Вне границ**: backoffice **не** разворачивается в артефакте `on_premise_box`; он живёт только на инфраструктуре вендора. Приватный Ed25519-ключ подписи лицензий **не** лежит в backoffice-приложении/БД (см. [`license_server.md`](./license_server.md)).

---

## 2. Принципы (зафиксированные решения)

1. **Отдельный контур**: собственный host/порт/`--app` и собственный rate-limit пул (`backoffice_*` в `Settings`). Не смешивать с тенантной админкой и публичным виджетом.
2. **Разделение `customer` ↔ `tenant`**: `customers` (юр. лицо/покупатель) отделён от `tenants` (тех. инсталляция/подписка). Связь `1 customer : N tenants` (cloud-тенант + box-инсталляции).
3. **Отдельная модель пользователей**: `backoffice_users` с ролями `BackofficeRole` (`superadmin`/`support`/`finance`/`sales`), **независимо** от тенантной `AdminRole`.
4. **Обязательная 2FA**: TOTP для всех backoffice-ролей (`backoffice_totp_required = true`); SSO — опционально; принцип наименьших привилегий.
5. **Cross-tenant только через break-glass**: любой вход сотрудника вендора в чужой тенант (impersonation/«войти как») — короткоживущий scope с **обязательным** аудитом каждого входа в `backoffice_audit_log`.
6. **Полный аудит мутаций**: все действия вендора пишутся в `backoffice_audit_log` в той же транзакции (`actor`, IP, UA, `impersonated_tenant_id`).
7. **Backoffice-данные вне tenant-RLS**: кросс-тенантные таблицы доступны только backoffice `service_role` (см. [`storage_schema.md`](./storage_schema.md) §2.2).
8. **Биллинг — внешний провайдер**: собственный биллинг не пишем; `orders` — минимальный каркас со ссылкой на внешнего провайдера (Stripe/Paddle/ручной). Минимизация ПД покупателя (152-ФЗ/GDPR, data residency — ПД отдельно от RAG-данных).
9. **Additive-only миграции**: новые таблицы/поля добавляются по регламенту `storage_schema.md` §5.

---

## 3. Роли (RBAC) backoffice

| Роль | Клиенты/заказы | Лицензии/коды | Коробки | Impersonation | Backoffice-пользователи |
|---|---|---|---|---|---|
| `superadmin` | CRUD | Выдача/отзыв/продление | CRUD | ✅ (break-glass) | Управление |
| `support` | Просмотр | Просмотр | Просмотр + heartbeat | ✅ (break-glass) | — |
| `finance` | Заказы, счета, продления | Продление/апгрейд | Просмотр | — | — |
| `sales` | Клиенты, заказы | Генерация кодов | Просмотр поставок | — | — |

- Доступ к данным тенантов (документы/чанки) для `finance`/`sales` **запрещён**; `support`/`superadmin` — только через break-glass impersonation с аудитом.

---

## 4. Модель данных (сводка)

Полные DDL — в [`storage_schema.md`](./storage_schema.md) §2.2; Pydantic-контракты — в [`modules_spec.md`](./modules_spec.md):

- `customers` — покупатели (юр. лица), отдельно от `tenants`.
- `backoffice_users` — сотрудники вендора (`BackofficeRole`, 2FA).
- `orders` — заказы/биллинг (каркас + внешний провайдер).
- `activation_codes` — коды `RAG-ENT-XXXX-YYYY-ZZZZ` под заказ.
- `box_deployments` — реестр проданных/развёрнутых коробок (образ, поставка, `hardware_id`, heartbeat, restricted).
- `backoffice_audit_log` — глобальный кросс-тенантный аудит + break-glass входы.
- Расширение `licenses`: `issued_by`, `customer_id`, `revoked_at`, `revocation_reason`.

---

## 5. Контракты эндпоинтов (`/api/v1/backoffice/*`)

| Метод | Путь | Запрос | Ответ | Назначение |
|---|---|---|---|---|
| `GET` | `/dashboard` | `?period=` | `VendorDashboard` | MRR/ARR, активные тенанты/коробки, просрочки, heartbeat-сводка |
| `GET` | `/customers` | `?status=&q=&page=` | `Page[Customer]` | Каталог покупателей |
| `POST` | `/customers` | `Customer` | `Customer` | Создание покупателя |
| `GET` | `/customers/{id}/tenants` | — | `List[TenantContext]` | Тенанты/инсталляции покупателя |
| `GET` | `/orders` | `?customer_id=&status=` | `Page[Order]` | Заказы/биллинг |
| `POST` | `/activation-codes` | `{customer_id, plan}` | `ActivationCode` | Генерация кода `RAG-ENT-...` |
| `GET` | `/activation-codes` | `?customer_id=&status=` | `Page[ActivationCode]` | Список/статусы кодов |
| `POST` | `/licenses/issue` | `LicenseIssueRequest` | `LicenseStatus` | Выдача лицензии (подпись в signing-сервисе) |
| `POST` | `/licenses/revoke` | `LicenseRevokeRequest` | `204` | Отзыв лицензии + публикация в CRL |
| `POST` | `/licenses/{id}/extend` | `{expires_at}` | `LicenseStatus` | Продление |
| `POST` | `/licenses/{id}/upgrade` | `{plan, limits, features}` | `LicenseStatus` | Апгрейд/даунгрейд плана |
| `GET` | `/box-deployments` | `?stale_hours=&customer_id=` | `Page[BoxDeployment]` | Реестр коробок, фильтр по «молчащим» heartbeat |
| `POST` | `/tenants/{id}/impersonate` | `ImpersonationGrant` | `ImpersonationGrant` | Break-glass вход (короткий scope, запись в аудит) |
| `GET` | `/audit-log` | `?actor=&action=&tenant=&page=` | `Page[AuditLogEntry]` | Глобальный аудит, включая impersonation |
| `GET`/`POST` | `/users` | `BackofficeUser` | — | Управление backoffice-пользователями (только `superadmin`) |

**Общие правила:**
- Все эндпоинты требуют валидную сессию backoffice + роль + 2FA; небезопасные методы — ещё и CSRF-токен.
- Все мутации пишут запись в `backoffice_audit_log` в той же транзакции.
- Impersonation выдаёт короткоживущий scope; факт входа и выхода логируется отдельно.

---

## 6. Приоритизация (волны поставки)

- **MVP (волна 1)**: роль `superadmin`, выдача/отзыв лицензий, кросс-тенантный аудит + impersonation, реестр инсталляций (`box_deployments` + heartbeat). Серверная половина — см. [`license_server.md`](./license_server.md).
- **Волна 2**: активационные коды, каталог клиентов, продление/апгрейд из панели, квоты/потребление per tenant.
- **Волна 3**: биллинг (внешний провайдер), customer portal (скачивание `.key`, заявки, статусы), дистрибуция образов и переактивация железа.

---

## 7. Безопасность и соответствие

- **Изоляция контура**: отдельный host/`--app`/пул, отдельная (рекомендуется) backoffice-БД/схема вне tenant-RLS.
- **Ключи лицензий**: подпись только offline Ed25519-ключом в signing-сервисе / HSM / KMS; ротация; revocation list (CRL). Приватный ключ не попадает в веб/БД backoffice и никогда — в артефакт Box.
- **Break-glass**: cross-tenant доступ только через impersonation с обязательным аудитом; минимальный scope и TTL.
- **ПД**: минимизация персональных данных покупателя, хранение отдельно от RAG-данных; учёт 152-ФЗ / GDPR / data residency; `telemetry_opt_out` уважается.
- **Миграции**: additive-only, версионирование схемы (см. `storage_schema.md` §5).

---

## 8. Чек-лист безопасности backoffice

- [x] Отдельный host/`--app`/rate-limit пул, не смешан с тенантной админкой.
- [x] Обязательная TOTP 2FA для всех backoffice-ролей.
- [x] Cross-tenant доступ только break-glass + аудит каждого входа.
- [x] Приватный ключ подписи лицензий вне веб/БД (signing-сервис / HSM / KMS).
- [x] Backoffice-таблицы вне tenant-RLS, доступ только backoffice `service_role`.
- [x] Полный аудит всех мутаций вендора в той же транзакции.
- [x] Биллинг через внешнего провайдера, минимизация ПД покупателя.
