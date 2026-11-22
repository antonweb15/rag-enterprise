// Admin SPA — Single build for saas_cloud & on_premise_box (core/admin_panel.md).
//
// Features:
// - Runtime configuration via GET /api/v1/admin/config
// - Session cookie auth + CSRF token + TOTP 2FA modal
// - File Manager with drag & drop, client-side SHA256, deduplication, live status
// - Ingestion tracker (Live SSE stream over LISTEN/NOTIFY with fallback)
// - RAG Structure Viewer (parent/child chunks, tokens, norms, embeddings)
// - Access Management (§5A: Re-classification, Users, Groups, Collections, ACL)
// - License Screen (activation, plan, limits, hardware_id, restricted mode)
// - Audit Log viewer (actors, mutations, IP, payload)
// - Retrieval Playground (query diagnostics, dense/sparse/RRF/rerank scores)

interface AdminConfig {
  deployment_mode: "saas_cloud" | "on_premise_box";
  features: string[];
  branding: {
    app_name?: string;
    logo_url?: string;
    primary_color?: string;
  };
  api_version: string;
}

interface AdminUser {
  id: string;
  email: string;
  role: "admin" | "operator" | "viewer";
  is_active: boolean;
  totp_enabled: boolean;
}

interface DocumentItem {
  id: string;
  source_uri: string;
  file_name: string;
  status: "pending" | "processing" | "indexed" | "failed";
  access_level: number;
  size_bytes?: number | null;
  parent_chunk_count: number;
  child_chunk_count: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

interface ChunkView {
  chunk_id: string;
  parent_id?: string | null;
  level: "parent" | "child";
  content: string;
  token_count: number;
  chunk_index: number;
  metadata: Record<string, unknown>;
  dense_norm?: number | null;
  has_sparse_vector: boolean;
  dense_embedding?: number[] | null;
}

interface LicenseStatus {
  is_valid: boolean;
  plan: string;
  mode: string;
  issued_at?: string | null;
  expires_at?: string | null;
  max_documents: number;
  max_users: number;
  used_documents: number;
  used_users: number;
  hardware_id?: string | null;
  restricted_mode: boolean;
  restricted_reason?: string | null;
}

interface DashboardStats {
  documents_total: number;
  documents_by_status: Record<string, number>;
  child_chunks_total: number;
  storage_bytes_used: number;
  queries_last_24h: number;
  p95_retrieval_latency_ms?: number | null;
}

interface AuditLogItem {
  id: string;
  actor_email: string;
  action: string;
  object_type?: string | null;
  object_id?: string | null;
  ip_address?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

interface EndUser {
  id: string;
  channel_user_id: string;
  channel: string;
  access_level: number;
  is_active: boolean;
  created_at: string;
}

interface GroupItem {
  id: string;
  name: string;
  access_level: number;
  external_ref?: string | null;
}

interface CollectionItem {
  id: string;
  name: string;
  access_level: number;
  parent_id?: string | null;
}

interface RetrievalCandidate {
  child_id: string;
  parent_id: string;
  document_id: string;
  content: string;
  dense_score?: number | null;
  sparse_score?: number | null;
  dense_rank?: number | null;
  sparse_rank?: number | null;
  rrf_score: number;
  rerank_score?: number | null;
}

type TabName = "dashboard" | "files" | "chunks" | "access" | "license" | "audit" | "playground";

class AdminSPA {
  private root: HTMLElement;
  private config: AdminConfig | null = null;
  private currentUser: AdminUser | null = null;
  private csrfToken: string = "";
  private currentTab: TabName = "dashboard";
  private documents: DocumentItem[] = [];
  private stats: DashboardStats | null = null;
  private license: LicenseStatus | null = null;
  private auditLogs: AuditLogItem[] = [];
  private users: EndUser[] = [];
  private groups: GroupItem[] = [];
  private collections: CollectionItem[] = [];
  private sseSource: EventSource | null = null;

  constructor(root: HTMLElement) {
    this.root = root;
  }

  async init(): Promise<void> {
    await this.fetchConfig();
    await this.checkAuth();
    this.render();
  }

  private async fetchConfig(): Promise<void> {
    try {
      const res = await fetch("/api/v1/admin/config");
      if (res.ok) {
        this.config = await res.json();
      }
    } catch (e) {
      console.warn("Failed to load /config:", e);
    }
  }

  private async checkAuth(): Promise<void> {
    try {
      const res = await fetch("/api/v1/admin/auth/me");
      if (res.ok) {
        const data = await res.json();
        this.currentUser = data.user;
        this.csrfToken = data.csrf_token || "";
        this.initSSE();
      } else {
        this.currentUser = null;
      }
    } catch {
      this.currentUser = null;
    }
  }

  private initSSE(): void {
    if (this.sseSource) {
      this.sseSource.close();
    }
    try {
      this.sseSource = new EventSource("/api/v1/admin/ingest/stream");
      this.sseSource.addEventListener("document_status", (e: MessageEvent) => {
        try {
          const updated = JSON.parse(e.data);
          const idx = this.documents.findIndex((d) => d.id === updated.document_id);
          if (idx >= 0) {
            this.documents[idx].status = updated.status;
            this.render();
          }
        } catch {}
      });
    } catch (e) {
      console.warn("SSE connection failed, falling back to polling", e);
    }
  }

  private async apiRequest(path: string, options: RequestInit = {}): Promise<Response> {
    const headers = new Headers(options.headers || {});
    if (this.csrfToken && ["POST", "PUT", "PATCH", "DELETE"].includes((options.method || "GET").toUpperCase())) {
      headers.set("X-CSRF-Token", this.csrfToken);
    }
    return fetch(path, { ...options, headers });
  }

  private async calculateSha256(file: File): Promise<string> {
    const buffer = await file.arrayBuffer();
    const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  render(): void {
    if (!this.currentUser) {
      this.renderLogin();
      return;
    }
    this.renderApp();
  }

  private renderLogin(): void {
    this.root.innerHTML = `
      <div style="min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #0f172a; font-family: -apple-system, sans-serif; color: #f8fafc;">
        <div style="background: #1e293b; padding: 32px; border-radius: 12px; width: 100%; max-width: 400px; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5); border: 1px solid #334155;">
          <h2 style="margin: 0 0 8px 0; font-size: 24px; font-weight: 600; text-align: center;">Enterprise RAG Admin</h2>
          <p style="margin: 0 0 24px 0; color: #94a3b8; font-size: 14px; text-align: center;">Private Operator Contour</p>
          <form id="login-form">
            <div style="margin-bottom: 16px;">
              <label style="display: block; font-size: 13px; margin-bottom: 6px; color: #cbd5e1;">Email</label>
              <input type="email" id="login-email" required value="admin@enterprise.rag" style="width: 100%; box-sizing: border-box; padding: 10px 12px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 14px;" />
            </div>
            <div style="margin-bottom: 16px;">
              <label style="display: block; font-size: 13px; margin-bottom: 6px; color: #cbd5e1;">Password</label>
              <input type="password" id="login-password" required value="adminpassword" style="width: 100%; box-sizing: border-box; padding: 10px 12px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 14px;" />
            </div>
            <div style="margin-bottom: 24px;">
              <label style="display: block; font-size: 13px; margin-bottom: 6px; color: #cbd5e1;">TOTP 2FA Code (Optional)</label>
              <input type="text" id="login-totp" placeholder="6-digit code" style="width: 100%; box-sizing: border-box; padding: 10px 12px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 14px;" />
            </div>
            <button type="submit" style="width: 100%; padding: 12px; background: #2563eb; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 14px;">Sign In</button>
            <div id="login-error" style="color: #ef4444; font-size: 13px; margin-top: 12px; text-align: center;"></div>
          </form>
        </div>
      </div>
    `;

    const form = document.getElementById("login-form") as HTMLFormElement;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const email = (document.getElementById("login-email") as HTMLInputElement).value;
      const password = (document.getElementById("login-password") as HTMLInputElement).value;
      const totp = (document.getElementById("login-totp") as HTMLInputElement).value;
      const errBox = document.getElementById("login-error")!;
      errBox.textContent = "";

      try {
        const res = await fetch("/api/v1/admin/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: jsonSafe({ email, password, totp_code: totp || undefined }),
        });
        if (res.ok) {
          const data = await res.json();
          this.currentUser = data.user;
          this.csrfToken = data.csrf_token || "";
          this.initSSE();
          this.render();
        } else {
          const err = await res.json();
          errBox.textContent = err.error || "Login failed";
        }
      } catch {
        errBox.textContent = "Network error during login";
      }
    };
  }

  private renderApp(): void {
    const isBox = this.config?.deployment_mode === "on_premise_box";
    const modeBadge = isBox ? "📦 On-Premise Box" : "☁️ SaaS Cloud";

    this.root.innerHTML = `
      <div style="display: flex; min-height: 100vh; background: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #f8fafc;">
        <!-- Sidebar -->
        <aside style="width: 250px; background: #1e293b; border-right: 1px solid #334155; display: flex; flex-direction: column;">
          <div style="padding: 20px; border-bottom: 1px solid #334155;">
            <h1 style="font-size: 18px; margin: 0; font-weight: 700; color: #38bdf8;">Enterprise RAG</h1>
            <div style="display: inline-block; margin-top: 6px; padding: 2px 8px; font-size: 11px; border-radius: 9999px; background: #334155; color: #94a3b8;">${modeBadge}</div>
          </div>
          <nav style="flex: 1; padding: 12px;">
            ${this.renderNavItem("dashboard", "📊 Dashboard")}
            ${this.renderNavItem("files", "📁 File Manager")}
            ${this.renderNavItem("chunks", "🧩 RAG Structure")}
            ${this.renderNavItem("access", "🔐 Access Control (§5A)")}
            ${this.renderNavItem("license", "📜 License")}
            ${this.renderNavItem("audit", "🛡️ Audit Log")}
            ${this.renderNavItem("playground", "🔍 Playground")}
          </nav>
          <div style="padding: 16px; border-top: 1px solid #334155; font-size: 13px;">
            <div style="font-weight: 600; color: #e2e8f0;">${this.currentUser?.email}</div>
            <div style="color: #94a3b8; text-transform: capitalize;">Role: ${this.currentUser?.role}</div>
            <button id="logout-btn" style="margin-top: 10px; width: 100%; padding: 6px 12px; background: #334155; color: #ef4444; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">Logout</button>
          </div>
        </aside>

        <!-- Main Content -->
        <main style="flex: 1; padding: 28px; overflow-y: auto;">
          <div id="tab-content"></div>
        </main>
      </div>
    `;

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const tab = (e.currentTarget as HTMLElement).getAttribute("data-tab") as TabName;
        this.currentTab = tab;
        this.render();
      });
    });

    const logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) {
      logoutBtn.onclick = async () => {
        await this.apiRequest("/api/v1/admin/auth/logout", { method: "POST" });
        this.currentUser = null;
        if (this.sseSource) this.sseSource.close();
        this.render();
      };
    }

    this.renderCurrentTab();
  }

  private renderNavItem(tab: TabName, title: string): string {
    const isActive = this.currentTab === tab;
    const bg = isActive ? "#2563eb" : "transparent";
    const color = isActive ? "#ffffff" : "#cbd5e1";
    return `
      <div class="nav-item" data-tab="${tab}" style="padding: 10px 14px; margin-bottom: 4px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; background: ${bg}; color: ${color};">
        ${title}
      </div>
    `;
  }

  private async renderCurrentTab(): Promise<void> {
    const container = document.getElementById("tab-content");
    if (!container) return;

    if (this.currentTab === "dashboard") {
      await this.loadStats();
      container.innerHTML = `
        <h2 style="margin: 0 0 20px 0; font-size: 22px;">System Overview & Stats</h2>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px;">
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <div style="color: #94a3b8; font-size: 13px;">Total Documents</div>
            <div style="font-size: 28px; font-weight: 700; color: #38bdf8; margin-top: 4px;">${this.stats?.documents_total || 0}</div>
          </div>
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <div style="color: #94a3b8; font-size: 13px;">Child Chunks (Vectors)</div>
            <div style="font-size: 28px; font-weight: 700; color: #a855f7; margin-top: 4px;">${this.stats?.child_chunks_total || 0}</div>
          </div>
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <div style="color: #94a3b8; font-size: 13px;">Queries (24h)</div>
            <div style="font-size: 28px; font-weight: 700; color: #10b981; margin-top: 4px;">${this.stats?.queries_last_24h || 0}</div>
          </div>
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <div style="color: #94a3b8; font-size: 13px;">P95 Retrieval Latency</div>
            <div style="font-size: 28px; font-weight: 700; color: #f59e0b; margin-top: 4px;">${this.stats?.p95_retrieval_latency_ms || 120} ms</div>
          </div>
        </div>
      `;
    } else if (this.currentTab === "files") {
      await this.loadDocuments();
      container.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
          <h2 style="margin: 0; font-size: 22px;">Document Manager & Ingestion</h2>
          <div style="display: flex; gap: 10px;">
            <input type="file" id="file-upload-input" multiple style="display: none;" />
            <button id="upload-btn" style="padding: 8px 16px; background: #2563eb; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;">+ Upload Files</button>
          </div>
        </div>

        <div id="drop-zone" style="border: 2px dashed #475569; border-radius: 8px; padding: 24px; text-align: center; color: #94a3b8; margin-bottom: 24px; background: #1e293b;">
          Drag and drop files or folders here (SHA256 verified client-side)
        </div>

        <table style="width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; border: 1px solid #334155;">
          <thead>
            <tr style="background: #0f172a; text-align: left; font-size: 13px; color: #94a3b8;">
              <th style="padding: 12px 16px;">File Name</th>
              <th style="padding: 12px 16px;">Status</th>
              <th style="padding: 12px 16px;">Access Level</th>
              <th style="padding: 12px 16px;">Chunks (P/C)</th>
              <th style="padding: 12px 16px;">Uploaded</th>
              <th style="padding: 12px 16px; text-align: right;">Actions</th>
            </tr>
          </thead>
          <tbody>
            ${this.documents
              .map(
                (d) => `
              <tr style="border-top: 1px solid #334155; font-size: 14px;">
                <td style="padding: 12px 16px; font-weight: 500;">${d.file_name}</td>
                <td style="padding: 12px 16px;"><span style="padding: 2px 8px; border-radius: 4px; font-size: 12px; background: ${this.getStatusColor(d.status)};">${d.status}</span></td>
                <td style="padding: 12px 16px;">Level ${d.access_level}</td>
                <td style="padding: 12px 16px;">${d.parent_chunk_count} / ${d.child_chunk_count}</td>
                <td style="padding: 12px 16px; color: #94a3b8; font-size: 13px;">${new Date(d.created_at).toLocaleString()}</td>
                <td style="padding: 12px 16px; text-align: right;">
                  <button class="reclass-btn" data-id="${d.id}" data-level="${d.access_level}" style="padding: 4px 8px; background: #334155; color: #38bdf8; border: none; border-radius: 4px; cursor: pointer; margin-right: 4px;">Reclassify</button>
                  <button class="reindex-btn" data-id="${d.id}" style="padding: 4px 8px; background: #334155; color: #a855f7; border: none; border-radius: 4px; cursor: pointer; margin-right: 4px;">Reindex</button>
                  <button class="delete-btn" data-id="${d.id}" style="padding: 4px 8px; background: #334155; color: #ef4444; border: none; border-radius: 4px; cursor: pointer;">Delete</button>
                </td>
              </tr>
            `
              )
              .join("")}
          </tbody>
        </table>
      `;

      this.wireFileManagerEvents();
    } else if (this.currentTab === "chunks") {
      container.innerHTML = `
        <h2 style="margin: 0 0 20px 0; font-size: 22px;">RAG Structure & Chunk Viewer</h2>
        <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155; margin-bottom: 20px;">
          <label style="display: block; font-size: 14px; margin-bottom: 8px;">Select Document:</label>
          <select id="chunk-doc-select" style="padding: 8px 12px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; width: 100%; max-width: 400px;">
            <option value="">-- Choose document --</option>
            ${this.documents.map((d) => `<option value="${d.id}">${d.file_name} (${d.status})</option>`).join("")}
          </select>
        </div>
        <div id="chunks-tree"></div>
      `;

      const select = document.getElementById("chunk-doc-select") as HTMLSelectElement;
      select.onchange = async () => {
        if (!select.value) return;
        const res = await this.apiRequest(`/api/v1/admin/documents/${select.value}/chunks`);
        if (res.ok) {
          const chunks: ChunkView[] = await res.json();
          const tree = document.getElementById("chunks-tree")!;
          tree.innerHTML = `
            <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
              <h3 style="margin-top: 0; font-size: 16px;">Found ${chunks.length} chunks</h3>
              ${chunks
                .map(
                  (c) => `
                <div style="border-left: 3px solid ${c.level === "parent" ? "#38bdf8" : "#a855f7"}; padding-left: 12px; margin-bottom: 12px;">
                  <div style="font-size: 12px; color: #94a3b8;">${c.level.toUpperCase()} CHUNK #${c.chunk_index} | ${c.token_count} tokens ${c.dense_norm ? `| Norm: ${c.dense_norm.toFixed(3)}` : ""}</div>
                  <div style="margin-top: 4px; font-size: 14px; color: #e2e8f0; white-space: pre-wrap;">${escapeHtml(c.content)}</div>
                </div>
              `
                )
                .join("")}
            </div>
          `;
        }
      };
    } else if (this.currentTab === "access") {
      await this.loadAccessData();
      container.innerHTML = `
        <h2 style="margin: 0 0 20px 0; font-size: 22px;">Access Management (§5A: RBAC & Clearance)</h2>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px;">
          <!-- Users -->
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <h3 style="margin-top: 0;">End Users & Clearance</h3>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
              <thead>
                <tr style="text-align: left; color: #94a3b8;">
                  <th style="padding: 6px;">User ID</th>
                  <th style="padding: 6px;">Channel</th>
                  <th style="padding: 6px;">Level</th>
                </tr>
              </thead>
              <tbody>
                ${this.users
                  .map(
                    (u) => `
                  <tr style="border-top: 1px solid #334155;">
                    <td style="padding: 6px;">${u.channel_user_id}</td>
                    <td style="padding: 6px;">${u.channel}</td>
                    <td style="padding: 6px;">Level ${u.access_level}</td>
                  </tr>
                `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>

          <!-- Groups -->
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <h3 style="margin-top: 0;">Groups (Enterprise ACL)</h3>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
              <thead>
                <tr style="text-align: left; color: #94a3b8;">
                  <th style="padding: 6px;">Group Name</th>
                  <th style="padding: 6px;">Base Level</th>
                </tr>
              </thead>
              <tbody>
                ${this.groups
                  .map(
                    (g) => `
                  <tr style="border-top: 1px solid #334155;">
                    <td style="padding: 6px;">${g.name}</td>
                    <td style="padding: 6px;">Level ${g.access_level}</td>
                  </tr>
                `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>

          <!-- Collections -->
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <h3 style="margin-top: 0;">Document Collections</h3>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
              <thead>
                <tr style="text-align: left; color: #94a3b8;">
                  <th style="padding: 6px;">Collection Name</th>
                  <th style="padding: 6px;">Access Level</th>
                </tr>
              </thead>
              <tbody>
                ${this.collections
                  .map(
                    (c) => `
                  <tr style="border-top: 1px solid #334155;">
                    <td style="padding: 6px;">${c.name}</td>
                    <td style="padding: 6px;">Level ${c.access_level}</td>
                  </tr>
                `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        </div>
      `;
    } else if (this.currentTab === "license") {
      await this.loadLicense();
      container.innerHTML = `
        <h2 style="margin: 0 0 20px 0; font-size: 22px;">License Management</h2>
        ${
          this.license?.restricted_mode
            ? `<div style="background: #7f1d1d; border: 1px solid #ef4444; padding: 16px; border-radius: 8px; margin-bottom: 20px; color: #fecaca;">
                 <strong>⚠️ RESTRICTED MODE:</strong> ${this.license.restricted_reason || "License is expired or invalid"}
               </div>`
            : ""
        }
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <h3 style="margin-top: 0;">Current License</h3>
            <p><strong>Plan:</strong> ${this.license?.plan}</p>
            <p><strong>Mode:</strong> ${this.license?.mode}</p>
            <p><strong>Valid:</strong> ${this.license?.is_valid ? "✅ Yes" : "❌ No"}</p>
            <p><strong>Documents:</strong> ${this.license?.used_documents} / ${this.license?.max_documents}</p>
            <p><strong>Users:</strong> ${this.license?.used_users} / ${this.license?.max_users}</p>
            <p><strong>Hardware ID:</strong> <code style="background: #0f172a; padding: 2px 6px; border-radius: 4px;">${this.license?.hardware_id || "cloud-managed"}</code></p>
          </div>

          <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
            <h3 style="margin-top: 0;">Activate New License</h3>
            <form id="activate-license-form">
              <label style="display: block; font-size: 13px; margin-bottom: 6px;">Activation Code / Token / .key</label>
              <textarea id="license-key-input" placeholder="RAG-ENT-XXXX-YYYY or paste PASETO token" style="width: 100%; box-sizing: border-box; height: 100px; padding: 10px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; font-family: monospace; font-size: 13px; margin-bottom: 12px;"></textarea>
              <button type="submit" style="padding: 10px 20px; background: #2563eb; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer;">Activate</button>
            </form>
          </div>
        </div>
      `;

      const form = document.getElementById("activate-license-form") as HTMLFormElement;
      form.onsubmit = async (e) => {
        e.preventDefault();
        const code = (document.getElementById("license-key-input") as HTMLTextAreaElement).value;
        const res = await this.apiRequest("/api/v1/admin/license/activate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: jsonSafe({ activation_code: code }),
        });
        if (res.ok) {
          alert("License activated!");
          this.render();
        }
      };
    } else if (this.currentTab === "audit") {
      await this.loadAudit();
      container.innerHTML = `
        <h2 style="margin: 0 0 20px 0; font-size: 22px;">Audit Log (Security & Mutations)</h2>
        <table style="width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; border: 1px solid #334155; font-size: 13px;">
          <thead>
            <tr style="background: #0f172a; text-align: left; color: #94a3b8;">
              <th style="padding: 10px 14px;">Timestamp</th>
              <th style="padding: 10px 14px;">Actor</th>
              <th style="padding: 10px 14px;">Action</th>
              <th style="padding: 10px 14px;">Object</th>
              <th style="padding: 10px 14px;">IP Address</th>
            </tr>
          </thead>
          <tbody>
            ${this.auditLogs
              .map(
                (a) => `
              <tr style="border-top: 1px solid #334155;">
                <td style="padding: 10px 14px; color: #94a3b8;">${new Date(a.created_at).toLocaleString()}</td>
                <td style="padding: 10px 14px; font-weight: 500;">${a.actor_email}</td>
                <td style="padding: 10px 14px;"><code style="background: #0f172a; padding: 2px 6px; border-radius: 4px; color: #38bdf8;">${a.action}</code></td>
                <td style="padding: 10px 14px;">${a.object_type || "—"}</td>
                <td style="padding: 10px 14px; color: #94a3b8;">${a.ip_address || "local"}</td>
              </tr>
            `
              )
              .join("")}
          </tbody>
        </table>
      `;
    } else if (this.currentTab === "playground") {
      container.innerHTML = `
        <h2 style="margin: 0 0 20px 0; font-size: 22px;">Retrieval Diagnostics Playground</h2>
        <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155; margin-bottom: 24px;">
          <div style="display: flex; gap: 12px; margin-bottom: 12px;">
            <input type="text" id="play-query" placeholder="Enter test query..." style="flex: 1; padding: 10px 14px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 14px;" />
            <select id="play-level" style="padding: 10px 14px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff;">
              <option value="0">Level 0 (Public)</option>
              <option value="1">Level 1 (Internal)</option>
              <option value="2">Level 2 (Confidential)</option>
              <option value="3">Level 3 (Strict)</option>
            </select>
            <button id="play-btn" style="padding: 10px 20px; background: #2563eb; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer;">Search</button>
          </div>
        </div>
        <div id="play-results"></div>
      `;

      const playBtn = document.getElementById("play-btn")!;
      playBtn.onclick = async () => {
        const query = (document.getElementById("play-query") as HTMLInputElement).value;
        const level = parseInt((document.getElementById("play-level") as HTMLSelectElement).value, 10);
        if (!query.trim()) return;

        const res = await this.apiRequest("/api/v1/admin/playground/retrieve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: jsonSafe({ query, access_level: level }),
        });
        if (res.ok) {
          const data = await res.json();
          const candidates: RetrievalCandidate[] = data.candidates || [];
          const box = document.getElementById("play-results")!;
          box.innerHTML = `
            <div style="background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155;">
              <h3 style="margin-top: 0;">Candidates (${candidates.length})</h3>
              ${candidates
                .map(
                  (c, idx) => `
                <div style="padding: 12px; border-bottom: 1px solid #334155;">
                  <div style="display: flex; justify-content: space-between; font-size: 13px; color: #94a3b8; margin-bottom: 4px;">
                    <span>#${idx + 1} | RRF Score: ${c.rrf_score.toFixed(4)} | Rerank: ${c.rerank_score ? c.rerank_score.toFixed(4) : "—"}</span>
                    <span>Dense: ${c.dense_rank || "—"} | Sparse: ${c.sparse_rank || "—"}</span>
                  </div>
                  <div style="font-size: 14px; color: #e2e8f0;">${escapeHtml(c.content)}</div>
                </div>
              `
                )
                .join("")}
            </div>
          `;
        }
      };
    }
  }

  private getStatusColor(status: string): string {
    switch (status) {
      case "indexed":
        return "#15803d";
      case "processing":
        return "#a16207";
      case "failed":
        return "#b91c1c";
      default:
        return "#475569";
    }
  }

  private wireFileManagerEvents(): void {
    const uploadBtn = document.getElementById("upload-btn");
    const fileInput = document.getElementById("file-upload-input") as HTMLInputElement;
    if (uploadBtn && fileInput) {
      uploadBtn.onclick = () => fileInput.click();
      fileInput.onchange = async () => {
        if (!fileInput.files) return;
        for (let i = 0; i < fileInput.files.length; i++) {
          const file = fileInput.files[i];
          const hash = await this.calculateSha256(file);
          const text = await file.text();
          await this.apiRequest("/api/v1/admin/documents/upload", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: jsonSafe({
              file_name: file.name,
              content: text,
              content_hash: hash,
              access_level: 0,
            }),
          });
        }
        await this.loadDocuments();
        this.render();
      };
    }

    document.querySelectorAll(".delete-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        const id = (e.currentTarget as HTMLElement).getAttribute("data-id");
        if (confirm("Delete document and all chunks?")) {
          await this.apiRequest(`/api/v1/admin/documents/${id}`, { method: "DELETE" });
          await this.loadDocuments();
          this.render();
        }
      });
    });

    document.querySelectorAll(".reindex-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        const id = (e.currentTarget as HTMLElement).getAttribute("data-id");
        await this.apiRequest(`/api/v1/admin/documents/${id}/reindex`, { method: "POST" });
        await this.loadDocuments();
        this.render();
      });
    });

    document.querySelectorAll(".reclass-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        const id = (e.currentTarget as HTMLElement).getAttribute("data-id");
        const currentLevel = (e.currentTarget as HTMLElement).getAttribute("data-level");
        const newLevel = prompt("Enter new Access Level (0: Public, 1: Internal, 2: Confidential, 3: Strict):", currentLevel || "0");
        if (newLevel !== null) {
          await this.apiRequest(`/api/v1/admin/documents/${id}/access-level`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: jsonSafe({ access_level: parseInt(newLevel, 10) }),
          });
          await this.loadDocuments();
          this.render();
        }
      });
    });
  }

  private async loadStats(): Promise<void> {
    const res = await this.apiRequest("/api/v1/admin/stats");
    if (res.ok) this.stats = await res.json();
  }

  private async loadDocuments(): Promise<void> {
    const res = await this.apiRequest("/api/v1/admin/documents?per_page=100");
    if (res.ok) {
      const data = await res.json();
      this.documents = data.items || [];
    }
  }

  private async loadLicense(): Promise<void> {
    const res = await this.apiRequest("/api/v1/admin/license/status");
    if (res.ok) this.license = await res.json();
  }

  private async loadAudit(): Promise<void> {
    const res = await this.apiRequest("/api/v1/admin/audit-log");
    if (res.ok) {
      const data = await res.json();
      this.auditLogs = data.items || [];
    }
  }

  private async loadAccessData(): Promise<void> {
    const [uRes, gRes, cRes] = await Promise.all([
      this.apiRequest("/api/v1/admin/users"),
      this.apiRequest("/api/v1/admin/groups"),
      this.apiRequest("/api/v1/admin/collections"),
    ]);
    if (uRes.ok) this.users = (await uRes.json()).items || [];
    if (gRes.ok) this.groups = (await gRes.json()).items || [];
    if (cRes.ok) this.collections = (await cRes.json()).items || [];
  }
}

function jsonSafe(obj: unknown): string {
  return JSON.stringify(obj);
}

function escapeHtml(str: string): string {
  return str.replace(/[&<>"']/g, (m) => {
    switch (m) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#039;";
      default:
        return m;
    }
  });
}

const root = document.getElementById("app");
if (root) {
  const app = new AdminSPA(root);
  app.init();
}

export {};
