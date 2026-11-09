# License Server (Issuance / Revocation / Heartbeat) Specification (`core/license_server.md`)

## 1. Subsystem purpose and boundaries

The **License Server** is the **server half of the vendor's licensing model**: issuance, revocation, receiving heartbeats and publishing the certificate revocation list (CRL). Previously the specs contained only client-side validation (`BaseLicenseValidator`) and the master-server URL (`license_server_url`) — the server itself was missing (noted in `deepseek-criticism.md` §8). This document fills in the missing half.

**Delineation from the existing parts:**

| Side | Where described | What it does |
|---|---|---|
| **Client (validator)** | `modules_spec.md` (`BaseLicenseValidator`), `admin_panel.md` §8 | Offline verification of the Ed25519 signature, expiry, `hardware_id`; self-service activation by code/`.key`/token; heartbeat ping |
| **Server (issuer)** | **this document**, `modules_spec.md` (`BaseLicenseServer`) | Issuance/revocation/renewal, receiving heartbeats, CRL — **vendor contour only** |

**Out of scope**: the License Server is **not** part of the `on_premise_box` artifact. Access to issuance is only through the Vendor Backoffice (see [`vendor_backoffice.md`](./vendor_backoffice.md)).

---

## 2. Principles (fixed decisions)

1. **Single signing source**: licenses are signed **only** with the vendor's offline Ed25519 key. The private key lives in a separate **signing service / HSM / KMS** (`license_signing_service_url`), and **not** in the web application or the backoffice DB.
2. **Separation of signing and sessions**: `LICENSE_SIGNING_KEY` is **never** reused to sign sessions (this invariant is already fixed in `admin_panel.md` §4.3).
3. **Two license profiles**:
   - `online_subscription` (Cloud/subscription): has `expires_at`, periodic heartbeat, online revocation check.
   - `airgap_enterprise` (Box/offline): bound to `hardware_id`, may be perpetual (`expires_at = NULL`), the revocation check uses a CRL snapshot when the network is available.
4. **Grace Period**: when the server/heartbeat is unavailable the installation works in grace mode (72h by default, aligned with `BaseLicenseValidator.verify_heartbeat`), after which — **Restricted Mode** with a message from the backend.
5. **Revocation List (CRL)**: revocation is published in the CRL (`license_crl_url`); when online the client checks the license against the CRL. When offline — against the last available snapshot.
6. **Key rotation**: support for several active public keys (key id in the token) for seamless rotation without mass re-activation.
7. **Full audit**: every issuance/revocation/extend operation is written to `backoffice_audit_log` (see [`storage_schema.md`](./storage_schema.md) §2.2).

---

## 3. Server contract (`BaseLicenseServer`)

The contract is defined in [`modules_spec.md`](./modules_spec.md):

- `issue_license(LicenseIssueRequest) -> license_token` — issue a signed Ed25519 token (signing is delegated to the signing service).
- `revoke_license(LicenseRevokeRequest)` — mark `revoked_at`/`revocation_reason` + publish to the CRL.
- `register_heartbeat(hardware_id, license_id)` — update `last_heartbeat_at`, compute grace/restricted.
- `generate_activation_code(customer_id, plan) -> ActivationCode` — generate a code `RAG-ENT-XXXX-YYYY-ZZZZ`.

---

## 4. Endpoint contracts

### 4.1. Issuance/management (private, Vendor Backoffice only)

| Method | Path | Request | Response | Purpose |
|---|---|---|---|---|
| `POST` | `/api/v1/backoffice/licenses/issue` | `LicenseIssueRequest` | `{license_token, license_id}` | Issue a signed token |
| `POST` | `/api/v1/backoffice/licenses/revoke` | `LicenseRevokeRequest` | `204` | Revoke + publish to the CRL |
| `POST` | `/api/v1/backoffice/licenses/{id}/extend` | `{expires_at}` | `LicenseStatus` | Renewal |
| `GET` | `/api/v1/backoffice/licenses/{id}/key-file` | — | `application/octet-stream` | Download the `.key` (airgap, `hardware_id` binding) |

### 4.2. Client exchange (public license master server)

| Method | Path | Request | Response | Purpose |
|---|---|---|---|---|
| `POST` | `/licenses/heartbeat` | `HeartbeatPayload` (`license_id`, `hardware_id`, `version`, anonymized aggregates) | `{status, grace_until, restricted}` | Periodic heartbeat from the installation |
| `GET` | `/licenses/crl` | `?since=` | `{revoked: [...], generated_at}` | Revocation list (CRL snapshot) |
| `POST` | `/licenses/reactivate` | `{license_id, old_hardware_id, new_hardware_id, reason}` | `LicenseStatus` | Re-activation on a hardware change (enterprise case) |

**Common rules:**
- Private endpoints (§4.1) require a backoffice role with issuance rights (`superadmin`/`finance`) + 2FA + CSRF.
- Public endpoints (§4.2) authenticate by license/`hardware_id`; the rate limit is the public policy.
- The heartbeat accepts only **anonymized aggregates** (`HeartbeatPayload`, see [`modules_spec.md`](./modules_spec.md)); when `telemetry_opt_out: true` the metric fields are ignored. Traces/documents/PII are not transmitted via heartbeat.

---

## 5. Hardware fingerprint and re-activation

- The current `hardware_id = sha256(cpu_id + mac)` is **unreliable** in Docker/K8s/VM (noted in `deepseek-criticism.md` §11-13). A composite fingerprint with several signals and a match threshold is recommended, as well as an explicit **re-activation** path (`/licenses/reactivate`) with an audit and a change limit.
- For `airgap_enterprise` the `.key` file is issued bound to `hardware_id`; re-activation on new hardware is done through the vendor (from the backoffice or a future-wave customer portal).

---

## 6. Security

- The private signing key is only in the signing service / HSM / KMS; the backoffice application invokes signing over a secure channel and does **not** store the key.
- Public keys (with a key id) are distributed in the artifacts for offline validation; rotation is done by adding a new key id without revoking old licenses.
- The CRL is signed with the same offline key; the client verifies the CRL signature before applying it.
- All issuance/revocation/re-activation operations have a full audit (`backoffice_audit_log`).

---

## 7. Prioritization

- **MVP (wave 1)**: `issue`/`revoke`, CRL, `heartbeat`, grace/restricted computation; integration of the signing service (or KMS) without a dedicated HSM.
- **Wave 2**: `extend`/`upgrade`, `.key` download, key rotation (key id).
- **Wave 3**: a robust fingerprint + self-service re-activation, image distribution and temporary access tokens for the private registry.
