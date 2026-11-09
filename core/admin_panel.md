# Admin Panel Specification (`core/admin_panel.md`)

## 1. Subsystem purpose and boundaries

The admin panel is the system's **private operator contour**: through it the tenant operator/owner manages the working document catalog (upload, delete, reindex), activates the license, and monitors the ingest queue, quotas and the audit log.

**Explicit delineation of the interfaces:**

| Contour | Audience | Access | Purpose |
|---|---|---|---|
| **Admin SPA** (`/api/v1/admin/*`) | Operators, tenant owners | Private, only after authentication | Document management, licenses (self-service), monitoring |
| **Vendor Backoffice** (`/api/v1/backoffice/*`) | Vendor staff (`superadmin`/`support`/`finance`/`sales`) | Private, separate vendor contour | Customers, license issuance/revocation, codes, box registry, cross-tenant support (see [`vendor_backoffice.md`](./vendor_backoffice.md)) |
| **Web Widget** (`/api/v1/widget/*`) | End users of the client's site | Public, anonymous session | Chat answers from the knowledge base |
| **Telegram Bot** (`BaseChannel`) | End users | Public channel | One of the answer-delivery channels (not the primary interface) |

**Out of scope**: the Telegram bot is **not** an administrative interface. Authorization by Telegram `user_id` / username provides no role model, 2FA, SSO or a full audit — using it for managing files and licenses is forbidden.

---

## 2. Principles (fixed decisions)

1. **One codebase, two modes**: `deployment_mode: saas_cloud | on_premise_box`. The differences are only through the runtime config `GET /api/v1/admin/config`, **no forks** of the frontend or backend.
2. **One DB for everything**: the admin panel is a consumer of the same PostgreSQL via `service_role`. There is no second "administrative" business DB.
3. **Three modular ports**: `SQLAlchemy 2.0` (relational: Postgres default / SQLite dev), `BaseVectorStore` (pgvector default / Qdrant / Chroma), `BaseObjectStorage` (S3 / MinIO / R2 / local, per-tenant). **RRF — in the application layer (code), not an SQL contract.**
4. **Authentication**: `fastapi-users` (SQLAlchemy) — email/password + TOTP 2FA + OAuth2/SSO. Transport — `CookieTransport` (HTTP-only + Secure + SameSite=Lax) + a CSRF token.
5. **Separation of key contours**: sessions — **HS256 with a per-install secret**; licenses — **Ed25519 with the vendor's offline key**. The keys are strictly independent.
6. **Frontend**: Vite + React + TypeScript + Tailwind v4 + shadcn/ui + Lucide + `recharts` (without Tremor). All licensing and quota logic — **only on the backend**.
7. **Real-time**: Postgres `LISTEN/NOTIFY` (fallback — polling `documents.status`). **In-memory pub/sub is forbidden.**
8. **Box security**: the admin panel listens on `127.0.0.1`, access via an SSH tunnel / an internal reverse proxy; the CLI (`typer`) duplicates license activation and ingest for air-gapped contours.
9. **Migrations**: `alembic upgrade head` on container start + a pre-upgrade backup hook + **additive-only** (destructive migrations are forbidden).
10. **RLS contract**: `event.listens_for(Session, "after_begin")` → `SET LOCAL app.current_tenant_id`. Without it RLS does not work with a connection pool and "leaks" between requests.

---

## 3. Subsystem architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│          Admin SPA (Vite + React + TS + Tailwind + shadcn/ui + recharts)     │
│   - File Manager (Drag & Drop of folders, client-side sha256, dedup)        │
│   - Ingestion Pipeline Tracker (Live SSE: pending → processing → indexed)   │
│   - RAG Structure Viewer (parent/child chunks, metadata, norms)             │
│   - License Screen (code / .key / PASETO, expiry, limits, hardware_id)      │
│   - Dashboard (recharts), Audit Log, Retrieval Playground                   │
│   * Licenses and quotas are only displayed: the bundle is readable, no checks on it │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ HTTPS, HTTP-only Secure Cookie + CSRF
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      FastAPI Admin Router (/api/v1/admin/*)                  │
│   - fastapi-users: email/password, TOTP 2FA, OAuth2/SSO, CookieTransport     │
│   - RBAC Dependency: admin | operator | viewer                              │
│   - Rate-limit policy: private (separate from the public widget)            │
│   - Serving the SPA static files (Box: one container) / Nginx-CDN (SaaS)    │
└───────────┬───────────────────────────┬───────────────────────┬─────────────┘
            │                           │                       │
            ▼                           ▼                       ▼
┌───────────────────────┐   ┌───────────────────────┐  ┌────────────────────┐
│ PostgreSQL (service_  │   │ Taskiq + Postgres     │  │ BaseObjectStorage  │
│ role, RLS, pgvector)  │   │ process_document_task │  │ (S3/MinIO/R2/local)│
│ + LISTEN/NOTIFY       │   │                       │  │ per-tenant prefix  │
└───────────────────────┘   └───────────────────────┘  └────────────────────┘
                                        │
                                        ▼
                             ┌────────────────────┐
                             │ BaseVectorStore    │
                             │ (pgvector default) │
                             └────────────────────┘
```

---

## 4. Authentication and authorization

### 4.1. Mechanism
- **Library**: `fastapi-users` on top of `SQLAlchemy 2.0` (the same DB, the same schema, the same code path for SaaS and Box — without an external GoTrue/Supabase Auth, which is impossible in air-gapped).
- **Login methods**: email + password (Argon2id), **TOTP 2FA** (Google Authenticator / 1Password), OAuth2 / OIDC SSO (a corporate provider) — optionally by `features`.
- **Session transport**: `CookieTransport` — a cookie with the flags `HttpOnly`, `Secure`, `SameSite=Lax`, a restricted `Path=/api/v1` and a TTL of 60 minutes (refresh — via a sliding update).
- **CSRF**: a double-submit cookie / an `X-CSRF-Token` header on all unsafe methods (`POST`, `PUT`, `PATCH`, `DELETE`).
- **Forbidden**: ⛔ **do not store the JWT in `localStorage` / `sessionStorage`** and do not pass it in `Authorization: Bearer` from the SPA. A single XSS in the admin panel = a full takeover of the contour with the right to delete data and replace the license.

### 4.2. Roles (RBAC)

| Role | Documents | License | Dashboard / Audit | Users |
|---|---|---|---|---|
| `admin` | CRUD + reindex | Activation / view | Full access | Management |
| `operator` | Upload / reindex (no delete) | Status view only | View | — |
| `viewer` | Only the list and chunks | Status view only | View | — |

- DB access: the admin process operates as `service_role` (BYPASSRLS), the key exists **only on the server**. Scoped access by `tenant_id` is provided by `SET LOCAL app.current_tenant_id` in `after_begin` (see `storage_schema.md`).
- The tenant admin-panel roles (`admin`/`operator`/`viewer`) do **not** include the vendor super-admin contour. Cross-tenant access, license issuance and customer management are moved to the **separate** Vendor Backoffice contour with the `BackofficeRole` roles (`superadmin`/`support`/`finance`/`sales`); a vendor employee's entry into a foreign tenant — only via break-glass impersonation with mandatory auditing of every entry. See [`vendor_backoffice.md`](./vendor_backoffice.md) and [`license_server.md`](./license_server.md).

### 4.3. Keys (critical)

| Key | Algorithm | Where it lives | Purpose |
|---|---|---|---|
| `JWT_SECRET` | **HS256**, ≥ 32 bytes | Per-install: generated on the first start, stored in the installation's secret store, rotated on deploy | Signing the session cookie tokens |
| `LICENSE_SIGNING_KEY` | **Ed25519** | Only at the vendor, offline; the private part **never** gets into the Box artifact | Signing license tokens |

> ⚠️ **Reusing `LICENSE_SIGNING_KEY` to sign sessions is forbidden.** In that case the compromise of any client instance gives an attacker the ability to **issue licenses on the vendor's behalf**. Only the *library* (`pynacl` / `cryptography`) and the code pattern are reused, not the key.
>
> Asymmetric session signing (EdDSA) and a custom `JWTStrategy` are **not introduced** until a separate auth service appears (sign/verify in different processes). The native HS256 from `fastapi-users` is sufficient.

---

## 5. Endpoint contracts (`/api/v1/admin/*`)

| Method | Path | Request | Response | Purpose |
|---|---|---|---|---|
| `GET` | `/config` | — | `AdminConfig` | Runtime config: `deployment_mode`, `features`, `branding` |
| `GET` | `/documents` | `?status=&q=&page=&per_page=` | `Page[DocumentListItem]` | Document list with pagination and a status filter |
| `POST` | `/documents/upload` | `multipart/form-data` (files/folders, `access_level`, `content_hash`) | `List[DocumentListItem]` | Upload to `BaseObjectStorage` + enqueue `process_document_task` |
| `DELETE` | `/documents/{id}` | — | `204` | Cascade delete: the storage object + `documents` + parent/child chunks + vectors |
| `POST` | `/documents/{id}/reindex` | — | `DocumentListItem` | Force reindex (reset the status to `pending`) |
| `GET` | `/documents/{id}/chunks` | `?include_vectors=false&chunk_id=` | `List[ChunkView]` | View the parent/child structure; vectors — only via an explicit flag |
| `PATCH` | `/documents/{id}/access-level` | `{access_level}` | `DocumentListItem` | **Re-classification**: change the document's level; the cascade in parent/child is transactional (the `cascade_access_level` trigger) |
| `GET` | `/license/status` | — | `LicenseStatus` | Expiry, plan, limits, the online/offline validation status, Restricted Mode |
| `POST` | `/license/activate` | `LicenseActivationRequest` | `LicenseStatus` | Activation by code, `.key` file or PASETO token |
| `GET` | `/license/machine-fingerprint` | — | `MachineFingerprint` | The installation's `hardware_id` for issuing a bound license |
| `GET` | `/stats` | `?period=` | `DashboardStats` | Documents, chunks, requests, latency, storage volume, quotas |
| `GET` | `/ingest/queue` | `?state=` | `List[IngestQueueItem]` | The current state of the Taskiq queue (pending/processing/failed, retry) |
| `GET` | `/ingest/stream` | — (SSE) | `text/event-stream` | Live ingest statuses via Postgres `LISTEN/NOTIFY` |
| `GET` | `/audit-log` | `?actor=&action=&page=` | `Page[AuditLogEntry]` | Action log: who, when, what, IP, User-Agent |
| `POST` | `/playground/retrieve` | `{query, access_level}` | `List[RetrievalCandidate]` | Retrieval diagnostics: dense/sparse ranks, RRF, rerank |

**Access-management endpoints** (see §5A; part is gated by the `enterprise_acl` license flag):

| Method | Path | Request | Response | Purpose |
|---|---|---|---|---|
| `GET`/`POST`/`PATCH`/`DELETE` | `/users` | `{channel, channel_user_id, access_level, is_active}` | `Page[...]` / `User` | CRUD of end users and granting the clearance level |
| `GET`/`POST`/`DELETE` | `/groups` | `{name, access_level, external_ref}` | `Page[Group]` / `Group` | CRUD of groups (`enterprise_acl`) |
| `POST`/`DELETE` | `/groups/{id}/members` | `{user_id}` | `204` | Users' membership in a group (`user_groups`) |
| `GET`/`POST`/`DELETE` | `/collections` | `{name, access_level, parent_id}` | `Page[Collection]` / `Collection` | CRUD of collections/knowledge bases (`collections`) |
| `POST`/`DELETE` | `/documents/{id}/acl` | `DocumentAclEntry` (`principal_type`, `principal_id`, `permission`) | `204` | Grant/revoke a fine-grained ACL (`enterprise_acl`) |

**Common rules:**
- All endpoints require a valid session cookie + a role; unsafe methods also require a CSRF token.
- All mutations write a record to `audit_log` in the same transaction.
- Errors — in a single format `{ "detail": {...}, "code": "..." }`; internal traces are not exposed outward.

---

## 5A. Access management (rights separation)

The section is available only to the `admin` role (see §4.2). All mutations are written to `audit_log` in the same transaction.

### 5A.1. Base level (always available)

- **Document re-classification**: a UI to change the `access_level` (0-3) of an existing document (`PATCH /documents/{id}/access-level`). Changing the level **cascadingly and transactionally** rewrites `parent_chunks.access_level`/`child_chunks.access_level` (the `cascade_access_level` trigger, see [`storage_schema.md`](./storage_schema.md) §2.3). Until the synchronization completes the document is treated fail-closed.
- **CRUD of end users and granting clearance**: creating/blocking the tenant's `users` records and setting their `access_level` (`/users`). The level is stored in the DB and recomputed on the backend; the client does not set it.

### 5A.2. Enterprise layer (behind the `enterprise_acl`/`collections` license flag)

- **Groups**: CRUD of `groups` + membership management (`user_groups`); a group may be mapped to an IdP (`external_ref`).
- **Collections (knowledge bases)**: CRUD of `collections` + binding documents (`document_collections`).
- **Fine-grained ACL**: grant/revoke `document_acl` for a `user`/`group` (`/documents/{id}/acl`). The grant/revoke takes effect **immediately** (the next request already sees the change, without cache lag).

### 5A.3. Audit

Every rights mutation writes `audit_log` with an `action` from the set: `document.reclassify`, `user.create`/`user.update`/`user.disable`, `acl.grant`/`acl.revoke`, `group.create`/`group.delete`, `group.member.add`/`group.member.remove`, `collection.create`/`collection.delete`. The record is written in the same transaction as the mutation itself.

---

## 6. File manager and ingest pipeline

1. Drag & Drop of files **and folders** (`webkitdirectory`) in the SPA; for each file the client computes `sha256` and pre-asks the backend about a duplicate — re-upload is not performed.
2. The file is saved via `BaseObjectStorage.put(tenant_id, ...)` with a per-tenant prefix (`{tenant_id}/...`), after which a record is created in `documents` with the `pending` status.
3. The task `process_document_task(tenant_id, storage_ref, content_hash, access_level)` is enqueued.
4. Document statuses: `pending → processing → indexed | failed` (in the UI the worker's sub-stages are additionally detailed: Docling parsing → Parent/Child splitter → BGE-M3 embeddings → writing to `BaseVectorStore`).
5. Deletion — strictly cascading and transactional: the storage object → `child_chunks` → `parent_chunks` → `documents` (DB cascades `ON DELETE CASCADE` + explicit vector deletion if the vector store is external).
6. Quotas (`max_documents`, storage volume) are checked **before** writing, on the backend, via `enforce_quota`.

---

## 7. RAG structure visualization

- `GET /documents/{id}/chunks` by default returns only `content`, metadata, `token_count`, `chunk_index` and the vector **norms** (`dense_norm`), as well as a flag of the sparse representation's presence.
- Embeddings are returned **only** with `?include_vectors=true` **and only for a single chunk** (`?chunk_id=...`). Reason: 1024 float × N chunks = megabytes of JSON per request.
- The UI shows the tree `document → parent_chunks → child_chunks` highlighting which chunks made it into the latest Playground output.

---

## 8. License screen

- Input fields: the activation code (`RAG-ENT-XXXX-YYYY-ZZZZ`), uploading a `.key` file, pasting a PASETO/JWT token.
- Displayed: the plan, `issued_at` / `expires_at`, limits (`max_documents`, `max_users`), current consumption, `hardware_id`, the mode (`airgap_enterprise` / `online_subscription`), the time of the last heartbeat and the remaining Grace Period.
- A **Restricted Mode** banner on license expiry or Grace Period exhaustion — with the text coming from the backend.
- ⛔ The frontend **does not store or validate** licenses: the React bundle is always readable, any `if (license.valid)` check on the client is decorative. The source of truth is the backend response.

---

## 9. Real-time statuses

- **Primary mechanism**: Postgres `LISTEN/NOTIFY` — the Taskiq worker on a document-status change sends `NOTIFY ingest_events, '{"document_id": ..., "status": ...}'`, the admin SSE endpoint listens on the channel and relays the events.
- **Fallback**: polling `documents.status` (interval 2-3 s) on connection start and in environments without `LISTEN` access.
- ⛔ **In-memory pub/sub is forbidden**: with `uvicorn --workers N` events from the Taskiq worker are not visible to the process serving the SSE connection.
- On the SSE stream start the client is given a snapshot of the current statuses, then — only deltas (`event: document_status`).

---

## 10. Deployment and isolation

- **Box**: the admin service binds to `127.0.0.1` (publishing the port outward is disabled by default). Access — an SSH tunnel or the client's internal reverse proxy with their own authentication.
- **Air-gapped CLI fallback** (`typer`) — so that in a closed contour a browser is not required at all:
  - `rag-admin license activate --file /etc/rag/license.key`
  - `rag-admin license status`
  - `rag-admin ingest --path ./data --access-level 0`
  - `rag-admin documents list|delete|reindex`
  - **The CLI fallback contract** (so that the "incomplete feature" does not diverge from REST):
    - The CLI calls the **same** service functions as the admin REST routers (a shared backend layer) — no parallel business logic.
    - Exit codes: `0` — success; `1` — a validation/input error; `2` — a license error (invalid/expired/`Restricted Mode`); `3` — an internal error.
    - Output format: human-readable by default; `--json` returns the same Pydantic models as REST (`LicenseStatus`, `IngestQueueItem`, `DashboardStats`), serialized to JSON.
    - Quotas and `access_level` are checked on the backend (`enforce_quota`, PDP) identically to REST; the CLI does not bypass the checks.
    - Every mutation writes `audit_log` (actor = the local CLI operator) just as from the UI.
- **Widget vs Admin**: one codebase, but **different `--app` / worker pools and different rate-limit policies**. The public high-load widget must not take down the private admin panel; the structure is ready for `docker compose scale widget=N` without refactoring.
- **SaaS**: the SPA is served by Nginx/CDN, the API by a separate service; **Box**: the SPA is served by FastAPI as `StaticFiles` from a single container.

---

## 11. Observability (Langfuse access)

The client reads traces **directly from the browser in the self-hosted Langfuse** (read-only, their own project). A duplicate read interface in the admin panel is **not built**.

### 11.1. Cloud: read-only client access

- The admin section shows the **non-secret**: `url` (the deep link `https://<langfuse-host>/project/<project_id>`) and `username` (`obs-<slug>@<domain>`).
- **The password is not stored and not shown permanently**: it is issued **once** on access creation; afterwards — a "Reset password" button.
- Endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/admin/observability/config` | `url`, `username`, `provisioned` (no secrets) |
| `POST` | `/api/v1/admin/observability/reset-password` | generates a new password, returns it **once** |

- The client's role in Langfuse: `None` at the organization + `Viewer` on their project → sees only themselves, read-only, does not see the project list (see [`eval_and_ops.md`](./eval_and_ops.md) §2.2).

### 11.2. Box: own Langfuse

- A local Langfuse is started in the box compose; the client is the **owner** (full access).
- `GET /api/v1/admin/observability/config` returns the local `url` + the initial admin login (one-time on the first start). After that the client manages users themselves in their Langfuse.

### 11.3. Invariant

- No licensing/quota logic and **no password** on the client / in the SPA config.
- The password is not stored in the DB (`tenants.config.observability`) — only `url`/`username`/`provisioned`.

---

## 12. Monorepo structure

```
rag-enterprise/
  apps/
    admin/      # Vite + React + TS + Tailwind v4 + shadcn/ui + Lucide + recharts
    widget/     # the embeddable web widget (Shadow DOM)
  services/
    api/        # FastAPI: admin / chat / widget routers, --app per contour
  packages/
    rag_core/   # Pydantic models + interfaces (BaseVectorStore, BaseObjectStorage, ...)
  core/         # The Markdown specification core (the source of truth)
```

- **Tooling**: `pnpm workspaces` (frontend) + a `uv` / `poetry` workspace (backend).
- **CI**: two independent pipelines (frontend: typecheck + build; backend: ruff + mypy + pytest + eval gate). The effort to set up the monorepo tooling is budgeted as a separate task — it does not fit within "Phase 1".

---

## 13. Admin-panel security checklist

- [x] HTTP-only + Secure + SameSite=Lax cookie, without `localStorage`.
- [x] A CSRF token on all mutations.
- [x] TOTP 2FA is mandatory for the `admin` role.
- [x] An audit of every mutation with IP and User-Agent.
- [x] Separated key contours (session HS256 per-install vs license Ed25519 vendor).
- [x] No licensing/quota logic on the client.
- [x] Box: bind `127.0.0.1` + the CLI fallback.
- [x] Separate rate-limit policies `admin` (private) and `widget` (public).
