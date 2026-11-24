# Vendor Backoffice (Superadmin) Specification (`core/vendor_backoffice.md`)

## 1. Subsystem purpose and boundaries

The **Vendor Backoffice** is a separate **vendor super-admin contour** through which vendor staff manage customers, licenses, activation codes, the box registry and cross-tenant support. It is **not** the tenant admin panel (`admin_panel.md`): they have different audiences, data, roles and security.

**The three system contours (delineation):**

| Contour | Audience | Access | Purpose |
|---|---|---|---|
| **Vendor Backoffice** (`/api/v1/backoffice/*`) | Vendor staff | Private, vendor only | Customers, license issuance, codes, boxes, cross-tenant support |
| **Tenant Admin SPA** (`/api/v1/admin/*`) | Tenant operators/owners | Private, after authentication | Documents, licenses (self-service), monitoring (see `admin_panel.md`) |
| **Web Widget / channels** (`/api/v1/widget/*`, `BaseChannel`) | End users | Public | Chat answers from the knowledge base (see `channels.md`) |

**Out of scope**: the backoffice is **not** deployed in the `on_premise_box` artifact; it lives only on the vendor infrastructure. The private Ed25519 license-signing key is **not** located in the backoffice application/DB (see [`license_server.md`](./license_server.md)).

---

## 2. Principles (fixed decisions)

1. **Separate contour**: its own host/port/`--app` and its own rate-limit pool (`backoffice_*` in `Settings`). Do not mix with the tenant admin panel or the public widget.
2. **Separation of `customer` ↔ `tenant`**: `customers` (legal entity/buyer) is separated from `tenants` (technical installation/subscription). The relationship is `1 customer : N tenants` (cloud tenant + box installations).
3. **Separate user model**: `backoffice_users` with `BackofficeRole` roles (`superadmin`/`support`/`finance`/`sales`), **independent** of the tenant `AdminRole`.
4. **Mandatory 2FA**: TOTP for all backoffice roles (`backoffice_totp_required = true`); SSO is optional; least-privilege principle.
5. **Cross-tenant only via break-glass**: any entry of vendor staff into a foreign tenant (impersonation / "log in as") is a short-lived scope with **mandatory** auditing of every entry in `backoffice_audit_log`.
6. **Full audit of mutations**: all vendor actions are written to `backoffice_audit_log` in the same transaction (`actor`, IP, UA, `impersonated_tenant_id`).
7. **Backoffice data outside tenant-RLS**: the cross-tenant tables are accessible only to the backoffice `service_role` (see [`storage_schema.md`](./storage_schema.md) §2.2).
8. **Billing - external provider**: we do not write our own billing; `orders` is a minimal skeleton with a link to an external provider (Stripe/Paddle/manual). Minimize buyer PII (152-FZ/GDPR, data residency - PII separate from RAG data).
9. **Additive-only migrations**: new tables/fields are added per the `storage_schema.md` §5 procedure.

---

## 3. Backoffice roles (RBAC)

| Role | Customers/orders | Licenses/codes | Boxes | Impersonation | Tenant lifecycle | Backoffice users |
|---|---|---|---|---|---|---|
| `superadmin` | CRUD | Issue/revoke/renew | CRUD | ✅ (break-glass) | Provision/suspend/offboard/backup/restore | Management |
| `support` | View | View | View + heartbeat | ✅ (break-glass) | — | — |
| `finance` | Orders, invoices, renewals | Renewal/upgrade | View | — | — | — |
| `sales` | Customers, orders | Code generation | View deliveries | — | — | — |

- Access to tenant data (documents/chunks) for `finance`/`sales` is **forbidden**; `support`/`superadmin` — only via break-glass impersonation with auditing.
- The SaaS tenant lifecycle capabilities (`tenant:provision`, `tenant:offboard`, `tenant:backup`) are held by `superadmin` **only** — they create/destroy whole installations and run juridical data deletion.

---

## 4. Data model (summary)

Full DDL is in [`storage_schema.md`](./storage_schema.md) §2.2; the Pydantic contracts are in [`modules_spec.md`](./modules_spec.md):

- `customers` — buyers (legal entities), separate from `tenants`.
- `backoffice_users` — vendor staff (`BackofficeRole`, 2FA).
- `orders` — orders/billing (skeleton + external provider).
- `activation_codes` — `RAG-ENT-XXXX-YYYY-ZZZZ` codes per order.
- `box_deployments` — registry of sold/deployed boxes (image, delivery, `hardware_id`, heartbeat, restricted).
- `backoffice_audit_log` — global cross-tenant audit + break-glass entries.
- Extension of `licenses`: `issued_by`, `customer_id`, `revoked_at`, `revocation_reason`.

---

## 5. Endpoint contracts (`/api/v1/backoffice/*`)

| Method | Path | Request | Response | Purpose |
|---|---|---|---|---|
| `GET` | `/dashboard` | `?period=` | `VendorDashboard` | MRR/ARR, active tenants/boxes, overdue items, heartbeat summary |
| `GET` | `/customers` | `?status=&q=&page=` | `Page[Customer]` | Buyer catalog |
| `POST` | `/customers` | `Customer` | `Customer` | Create a buyer |
| `GET` | `/customers/{id}/tenants` | — | `List[TenantContext]` | The buyer's tenants/installations |
| `GET` | `/orders` | `?customer_id=&status=` | `Page[Order]` | Orders/billing |
| `POST` | `/activation-codes` | `{customer_id, plan}` | `ActivationCode` | Generate a `RAG-ENT-...` code |
| `GET` | `/activation-codes` | `?customer_id=&status=` | `Page[ActivationCode]` | List/statuses of codes |
| `POST` | `/licenses/issue` | `LicenseIssueRequest` | `LicenseStatus` | Issue a license (signing in the signing service) |
| `POST` | `/licenses/revoke` | `LicenseRevokeRequest` | `204` | Revoke a license + publish to the CRL |
| `POST` | `/licenses/{id}/extend` | `{expires_at}` | `LicenseStatus` | Renewal |
| `POST` | `/licenses/{id}/upgrade` | `{plan, limits, features}` | `LicenseStatus` | Plan upgrade/downgrade |
| `GET` | `/box-deployments` | `?stale_hours=&customer_id=` | `Page[BoxDeployment]` | Box registry, filtered by "silent" heartbeats |
| `POST` | `/tenants/{id}/impersonate` | `ImpersonationGrant` | `ImpersonationGrant` | Break-glass entry (short scope, written to the audit) |
| `GET` | `/audit-log` | `?actor=&action=&tenant=&page=` | `Page[AuditLogEntry]` | Global audit, including impersonation |
| `GET`/`POST` | `/users` | `BackofficeUser` | — | Managing backoffice users (`superadmin` only) |

**Common rules:**
- All endpoints require a valid backoffice session + role + 2FA; unsafe methods also require a CSRF token.
- All mutations write a record to `backoffice_audit_log` in the same transaction.
- Impersonation issues a short-lived scope; entry and exit are logged separately.

---

### 5.1. Cross-tenant observability (Langfuse)

The super-admin sees all cloud projects in **a single self-hosted Langfuse** (role `Admin`/`Owner` at the organization; login via SSO, every action in `backoffice_audit_log`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/backoffice/tenants/{id}/observability/provision` | Create/re-issue access: project `tenant-<slug>` + an `Ingestion` write key + a `Viewer` user (org=`None`). The password is shown **once** |
| `POST` | `/api/v1/backoffice/tenants/{id}/observability/reset-password` | Force-reset the observer's password (one-time display) |
| `GET` | `/api/v1/backoffice/observability/tenants` | Summary: projects, `provisioned` status, last login |

Provisioning goes through the Langfuse **Org API / SCIM** (self-hosted; initially manual in the UI is acceptable). Only `{ url, username, provisioned }` is written to `tenants.config.observability` — **the password is not stored**. The programmatic lifecycle path (§5.2 `/tenants/provision`) drives the same project creation through the `LangfuseProvisioner` port, which returns exactly that `ObservabilityConfig` shape; the one-time `reset-password` is an interactive-only operation of this subsection. The box registry (`box_deployments`) is extended with `telemetry_opt_out` / `observability_forward_enabled`; forwarding from Box to the vendor Langfuse is strictly opt-in.

---

### 5.2. SaaS tenant lifecycle (`superadmin` only)

Atomic provisioning / suspension / juridical offboarding / backup-restore of a cloud tenant. The DB state is written in one transaction and committed **first**; irreversible external side-effects (the Langfuse project, the S3 object prefix) run **after** commit (compensated on failure), so a committed DB can never diverge from an un-rolled-back external resource.

| Method | Path | Request | Response | Capability | Purpose |
|---|---|---|---|---|---|
| `POST` | `/tenants/provision` | `TenantProvisionRequest` | `TenantProvisionResult` | `tenant:provision` | Create an isolated tenant (tenant row + signed license + Langfuse project + bootstrap admin), activate the customer |
| `POST` | `/tenants/{id}/suspend` | `{suspend?, reason?}` | `204` | `tenant:offboard` | Pause/resume a tenant (`tenants.is_active`) without destroying data |
| `GET` | `/tenants/{id}/export` | — | `TenantExport` | `tenant:offboard` | Full data export handed to the client (152-FZ/GDPR) |
| `POST` | `/tenants/{id}/offboard` | `TenantOffboardRequest` | `TenantOffboardResult` | `tenant:offboard` | Suspend → export → juridical hard-delete (DB) → purge S3/Langfuse; churn the customer only when it has no remaining active installation (`1 customer : N tenants`) |
| `POST` | `/tenants/{id}/backup` | — | `TenantBackupResult` | `tenant:backup` | On-demand DB-only tenant backup via the backup backend |
| `POST` | `/tenants/restore` | `TenantRestoreRequest` | `TenantRestoreResult` | `tenant:backup` | Restore a tenant's DB rows from a backup (refuses to clobber a live tenant unless `overwrite=true`) |

**Notes:**
- **Hard-delete is application-layer**, not DB `ON DELETE CASCADE`: rows are deleted children-first in a deterministic FK-safe order, which is portable across SQLite/Postgres and yields a precise deletion receipt. The surviving `backoffice_audit_log` entry is that receipt.
- **Backup/restore is DB-scoped**: it round-trips tenant rows (incl. `source_uri` strings) but does **not** re-create S3 objects or the Langfuse project. Object-storage/observability restore and a scheduled/PITR backup path are out of the current scope (ports exist; see `lifecycle.py`).
- The external effects (`LangfuseProvisioner`, `StoragePurger`, `TenantBackupBackend`) are injectable ports; the default `TenantBackupBackend` is in-memory (tests/dev). Real adapters (Langfuse Org API / boto3 / Supabase PITR) wire in without touching core logic.

---

## 6. Prioritization (delivery waves)

- **MVP (wave 1)**: the `superadmin` role, license issuance/revocation, cross-tenant audit + impersonation, the installation registry (`box_deployments` + heartbeat). The server half — see [`license_server.md`](./license_server.md).
- **Wave 2**: activation codes, the customer catalog, renewal/upgrade from the panel, per-tenant quotas/consumption.
- **Wave 3**: billing (external provider), a customer portal (`.key` download, requests, statuses), image distribution and hardware re-activation.

---

## 7. Security and compliance

- **Contour isolation**: a separate host/`--app`/pool, a separate (recommended) backoffice DB/schema outside tenant-RLS.
- **License keys**: signing only with the offline Ed25519 key in the signing service / HSM / KMS; rotation; revocation list (CRL). The private key does not get into the backoffice web/DB and never into the Box artifact.
- **Break-glass**: cross-tenant access only via impersonation with mandatory auditing; minimal scope and TTL.
- **PII**: minimize buyer personal data, store it separately from RAG data; account for 152-FZ / GDPR / data residency; `telemetry_opt_out` is respected.
- **Migrations**: additive-only, schema versioning (see `storage_schema.md` §5).

---

## 8. Backoffice security checklist

- [x] Separate host/`--app`/rate-limit pool, not mixed with the tenant admin panel.
- [x] Mandatory TOTP 2FA for all backoffice roles.
- [x] Cross-tenant access only via break-glass + an audit of every entry.
- [x] The private license-signing key is outside the web/DB (signing service / HSM / KMS).
- [x] Backoffice tables outside tenant-RLS, access only for the backoffice `service_role`.
- [x] Full audit of all vendor mutations in the same transaction.
- [x] Billing through an external provider, minimization of buyer PII.
