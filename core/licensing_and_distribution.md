# Licensing, Distribution & Multi-Tenancy Specification (`core/licensing_and_distribution.md`)

## 1. The hybrid model concept: SaaS Cloud vs On-Premise Box

The system supports two deployment modes with a shared core:

1. **SaaS Cloud (Managed Multi-Tenant)**:
   - Hosting on the developer's own servers (VPS / Docker Compose without a mandatory Kubernetes to cut infrastructure cost).
   - A single PostgreSQL 15+ / pgvector database (Supabase is one hosting option, not a mandatory dependency) with strict client isolation via `tenant_id` and Row-Level Security (RLS).
   - Client connection via the embeddable Web widget (`<script>`), a Telegram bot or the REST/SSE API.
   - Document storage: S3 (Cloudflare R2 / MinIO / Selectel S3 / AWS) with prefixes `s3://bucket/{tenant_id}/...`.

2. **On-Premise Box (Protected Enterprise)**:
   - Deployment in the customer's isolated contour (Docker Compose / Helm / K8s).
   - The client provides the compute resources, a local DBMS, S3 storage (MinIO/Ceph) and LLM providers (local Ollama or corporate API keys) themselves.
   - Intellectual property protection: the core is delivered as compiled C extensions (`.so` / Cython / PyArmor) via a private Docker Registry.
   - License validation: cryptographic offline verification (Ed25519) + a periodic online Heartbeat.

---

## 2. Code protection and distribution of the On-Premise box

```
┌────────────────────────────────────────────────────────┐
│             Core source code (Python Core)            │
└──────────────────────────┬─────────────────────────────┘
                           │ CI/CD Pipeline (GitHub Actions)
                           ▼
┌────────────────────────────────────────────────────────┐
│    Compilation and Obfuscation (Cython / PyArmor 8+)  │
│    - RAG Core / RRF Pipeline / Chunking Engine -> .so  │
│    - License Validator & Cryptographic Guard -> .so    │
└──────────────────────────┬─────────────────────────────┘
                           │ Multi-stage Docker Build
                           ▼
┌────────────────────────────────────────────────────────┐
│     Private Docker Registry (ghcr.io / Harbor)         │
│     - Image: rag-core:enterprise-vX.Y (no .py files)   │
│     - Client access via a temporary registry token     │
└────────────────────────────────────────────────────────┘
```

### Protection levels:
- **Critical core**: the licensing modules, the RRF algorithms, context budgeting and chunking are compiled into binary modules (`.so`).
- **Open layer**: the configuration controllers and the FastAPI schemas remain open to simplify client integration.
- **Restrictions on license expiry (Graceful Lockout / Restricted Mode)**:
  - On a missing or expired license the core blocks RAG-search execution or switches to a demo mode (limit: 5 documents, FlashRank disabled, watermarks in answers).

---

## 3. Cryptographic license-token system (Ed25519)

A license token is a compact token cryptographically signed with an asymmetric key (Ed25519). **The canonical format is `PASETO v4.public`** (an unambiguous cryptographic binding of version/purpose, no "alg confusion"). `JWT (EdDSA)` is allowed **only** as a compatible option for integrations requiring JWT; on a single installation exactly one format is used, chosen by the vendor at issuance. The short `Activation Code` like `RAG-ENT-...` is NOT a token, but a one-time code exchanged for a token (see §4.1).

### 3.1. Payload structure:
```json
{
  "license_id": "987fbc82-9a1b-4c3d-8e5f-0123456789ab",
  "client_id": "company_abc",
  "plan": "enterprise_box",
  "mode": "airgap_enterprise",
  "issued_at": "2026-10-26T10:00:00Z",
  "expires_at": "2027-10-26T10:00:00Z",
  "max_documents": 50000,
  "max_users": 100,
  "hardware_id": "sha256(cpu_id + mac_addr)",
  "telemetry_opt_out": true,
  "features": [
    "hybrid_search",
    "s3_connector",
    "flashrank_rerank",
    "web_widget",
    "enterprise_acl",
    "collections",
    "sso_oidc"
  ]
}
```
*Note: for perpetual plans the `expires_at` field is set to `null`.*

### 3.1a. Access-control feature flags (Stage B/C)

The flags gate **functionality**, not code (the code is single, no forks). The check is only on the backend.

| Flag | What it enables | When disabled |
|---|---|---|
| `enterprise_acl` | Groups (`groups`/`user_groups`) + fine-grained `document_acl` + `PolicyEvaluator` considers ACL/groups | Only the flat `access_level` (0-3) works |
| `collections` | Collections/knowledge bases (`collections`/`document_collections`) and their management UI | Collections are hidden |
| `sso_oidc` | The authenticated widget mode (OIDC/SAML → a JWT with `subject + groups`, `Settings.oidc_*`) | The widget is anonymous only (`access_level = 0`) |

The base dual-contour model (`tenant_id` + `access_level` + RLS) is **always enabled** and not gated by a flag (see [`storage_schema.md`](./storage_schema.md) §2.3-§2.4, [`modules_spec.md`](./modules_spec.md)).

> **The single feature-flag registry is `enum LicenseFeature`** ([`modules_spec.md`](./modules_spec.md)): `web_widget`, `telegram`, `rest_sse`, `cli`, `enterprise_acl`, `collections`, `sso_oidc`, `hybrid_search`, `s3_connector`, `flashrank_rerank`, `observability_forward`. This same set is the only one allowed for `licenses.features` (a CHECK constraint via `<@`, [`storage_schema.md`](./storage_schema.md)). No other values can be in `features`.
>
> ⚠️ **`observability_forward` vs `observability_forward_enabled`:** `observability_forward` is an **entitlement** (a license right, an element of `features`), while `observability_forward_enabled` is the client's **runtime switch** (`Settings`/`BoxDeployment`). The actual forwarding to the vendor is enabled only when all are true simultaneously: the entitlement in the license **AND** `observability_forward_enabled = true` **AND** `telemetry_opt_out = false`.

### 3.2. License modes and dual-contour validation:
1. **`airgap_enterprise` mode (isolated contour)**:
   - Fully autonomous operation without outbound network connections.
   - A public key (`Public Key`) is embedded into the core. The server verifies the `Ed25519` digital signature, the `hardware_id` match and `expires_at` strictly locally.
   - The online heartbeat is fully disabled.
2. **`online_subscription` mode (subscription model)**:
   - Offline Ed25519 signature verification + a periodic online Heartbeat (every 24 hours).
   - **Grace Period (72 hours)**: on a temporary internet outage the box continues full operation for 72 hours, writing a warning to the system log.
   - After the Grace Period the core switches to `Restricted Mode` (limited search, watermarks, a document limit).
3. **Telemetry security (Data Privacy & Compliance)**:
   - By default the `telemetry_opt_out: true` flag is enabled (fully disabling telemetry sending for 152-FZ / GDPR).
   - With telemetry active, a **strictly closed (allowlist) and exhaustive** set of anonymized aggregated technical fields is transmitted, matching `HeartbeatPayload` ([`modules_spec.md`](./modules_spec.md)):
     - `license_id`, `hardware_id` — installation identification (not PII);
     - `version` (the box's `image_version`);
     - `requests_last_period` — a request counter for the period;
     - `uptime_seconds` — uptime;
     - `p95_latency_ms` — aggregated latency.
   - Transmitting any field outside this list is forbidden. The texts of user queries, document content and personal data are **under no circumstances transmitted**. Disabling — via the `telemetry_opt_out: true` flag (the default value); forwarding to the vendor Langfuse additionally requires `observability_forward_enabled: false → true` (opt-in, see below).
   - **Observability (Langfuse)**: in Box its **own local Langfuse** is started (in the box compose); its data does not leave the contour. Forwarding traces/metrics to the vendor Langfuse is a separate **license feature flag** (`observability_forward`) and is possible **only** when `telemetry_opt_out: false`, strictly in anonymized form (no query texts, documents or PII).

---

## 4. User activation and administration scenario

### 4.1. Separation of concepts: Activation Code vs License Token
- **Activation Code**: a short alphanumeric key like `RAG-ENT-XXXX-YYYY-ZZZZ`, generated upon order payment. It is used for the box's first request to the master license server to obtain a signed token.
- **License Token**: a full cryptographically Ed25519-signed JWT/PASETO containing the entire payload and the public certificate. For air-gapped environments the client downloads a ready token file from their account.

### 4.2. Input and activation methods:
- **For SaaS Cloud**:
  - The owner enters the token/code in the Web admin panel or sends it to the owner's bot: `/activate RAG-ENT-...`.
  - The master system validates the key and updates the tenant limits in the `tenants` table.
- **For On-Premise Box**:
  - **File/environment variable**: `LICENSE_TOKEN=eyJhbGci...` or the path `LICENSE_FILE_PATH=/etc/rag/license.key` in `docker-compose.yml`.
  - **Local web admin panel**: the `/admin/license` page with the ability to upload a `.key` file or enter an online activation code.

### 4.3. Separation of the cryptographic contours (License vs Session)

The license and session keys are two **strictly independent** cryptographic contours. Reusing them is forbidden.

| Key | Algorithm | Where it lives | Purpose |
|---|---|---|---|
| `LICENSE_SIGNING_KEY` | **Ed25519** | Only at the vendor, offline; the private part **never** gets into the Box artifact (only the public key is embedded into the core) | Signing license tokens |
| `JWT_SECRET` | **HS256**, ≥ 32 bytes | Per-install: generated on the instance's first start, rotated on deploy | Signing the admin's session cookie tokens (`fastapi-users`) |

> ⚠️ **Reusing `LICENSE_SIGNING_KEY` to sign sessions is forbidden.** In that case the compromise of any client instance would allow issuing licenses on the vendor's behalf. Only the *library* (`pynacl` / `cryptography`) and the code pattern are reused, not the key.

### 4.4. Activation via the CLI (air-gapped)
For isolated contours where the admin web port is not open, activation is duplicated by the CLI (`typer`): `rag-admin license activate --file /etc/rag/license.key` and `rag-admin ingest --path /data/docs`. No browser needed.

### 4.5. The frontend does not validate licenses
The React bundle is always readable, so **all licensing and quota logic lives only on the backend**. The SPA only displays the status received from the server; any `if (license.valid)` check on the client is decorative and is not a protection mechanism.

---

## 4a. Hidden marks for leak attribution (Fingerprinting / Watermarking)

Hidden marks do **not prevent** copying — they provide **attribution** and legal proof after the fact (who is the source of the leaked / pirated copy). The implementation is the `rag_core.watermark` package (lives only in the obfuscated backend core; the frontend does not touch the marks, see §4.5).

### The key principle — binding to the license
All marks are deterministically derived from the token fields (§3.1) and a secret vendor key:

```
fingerprint = HMAC-SHA256(vendor_key, license_id | client_id | build_id)
```

`vendor_key` is a **separate** vendor secret, it must not be reused with `LICENSE_SIGNING_KEY` (§4.3). The client cannot forge someone else's fingerprint, and the vendor can always reproduce the mark from a leak.

### Mark layers (multi-layered — removing one layer does not kill the others)

| Layer | Module | Where it "leaks" | Purpose |
|---|---|---|---|
| Zero-width watermark | `zero_width` | RAG answer texts, exports | Invisible characters (`U+200B/200C/200D/FEFF`) encode the fingerprint; read by a script from the leaked text |
| Linguistic watermark | `linguistic` | Answer texts | Deterministic synonym choice by fingerprint bits; survives "cleaning" of invisible characters |
| Canary records (honeytokens) | `canary` | Index / knowledge base | Unique fake trap documents; surfacing in a foreign product = direct proof |
| Per-build stamp | `build_stamp` | `.so` / Docker image | A signed `client_id/build_id` stamp in the artifact: different clients = different hashes |
| Heartbeat fingerprint | `heartbeat` | Master server (`online_subscription`, §3.2) | A fingerprint + `hardware_id` in the heartbeat; one `license_id` from different `hardware_id` → a cloning signal |
| Vendor registry | `registry` | At the vendor, offline | A `license_id → fingerprint` registry for deterministic reproduction of the mark (proof for court) |

### Critical rules
1. The mark generation/verification logic is only in the obfuscated core (the frontend is readable, §4.5).
2. The fingerprint key is a separate vendor secret, not `LICENSE_SIGNING_KEY` (§4.3).
3. **Silent detection**: do not signal the client about the presence of marks and do not block on them immediately — otherwise they will be cleaned out; evidence is collected silently.
4. A beacon/heartbeat with a mark — only anonymized, respecting `telemetry_opt_out` and 152-FZ / GDPR (§3.2) and with a legal clause in the offer.
5. The fact of applying hidden marking is fixed in the license agreement.

---

## 5. The embeddable Web widget (link)

> **The single source** on the Web widget (the embed `<script>`, `data-api-url`, the two-step Session Handshake, the Origin allowlist without wildcard, Shadow DOM, the Cloud/Box modes) is [`channels.md`](./channels.md) §4-§6. Duplicating the description here is forbidden, to avoid breeding desync.

The licensing invariant of this document (stays here):
- The `web_widget` channel is a **license feature flag** (`features: [..., "web_widget"]`, see §3.1). The widget's availability is determined by the tenant's license.
- **The client (the widget's React bundle) does not validate the license** and contains no quota logic — see §4.5. Any license check is performed only on the backend.
