// Web widget - Shadow DOM embeddable component (core/channels.md §5).
//
// Features:
// - Shadow DOM encapsulation isolating CSS and DOM from the host site;
// - data-api-url mode handling (ignored in cloud mode, applied in box mode);
// - Two-step Session Handshake (POST /api/v1/widget/session -> JWT);
// - Typed white-label branding (GET /api/v1/widget/config);
// - Streaming chat responses over SSE (POST /api/v1/chat/stream);
// - Interactive suggested questions, citations display, and thumbs feedback.

interface WidgetConfig {
  slug: string;
  tenant_id: string;
  deployment_mode?: string;
  branding: {
    title?: string;
    subtitle?: string;
    logo_url?: string;
    primary_color?: string;
    welcome_message?: string;
    disclaimer?: string;
    suggested_questions?: string[];
  };
  capabilities: {
    supports_streaming: boolean;
    supports_markdown: boolean;
    supports_citations: boolean;
    supports_feedback_buttons: boolean;
  };
}

interface Citation {
  citation_index: number;
  document_title: string;
  source_uri: string;
  page?: number | null;
}

class RagWidgetElement extends HTMLElement {
  private shadow: ShadowRoot;
  private slug: string = "";
  private apiUrl: string = "";
  private hostOrigin: string = "";
  private isBoxMode: boolean = false;
  private sessionToken: string | null = null;
  private config: WidgetConfig | null = null;
  private isOpen: boolean = false;
  private messagesContainer!: HTMLElement;
  private inputField!: HTMLInputElement;

  constructor() {
    super();
    this.shadow = this.attachShadow({ mode: "open" });
  }

  connectedCallback(): void {
    const script = document.currentScript as HTMLScriptElement | null;
    const scriptSlug = script?.getAttribute("data-tenant-slug");
    const scriptApiUrl = script?.getAttribute("data-api-url");
    const scriptMode = script?.getAttribute("data-mode");

    this.slug = this.getAttribute("data-tenant-slug") || scriptSlug || "";
    this.isBoxMode = (this.getAttribute("data-mode") || scriptMode) === "box";

    this.hostOrigin = window.location.origin;
    const rawApiUrl = this.getAttribute("data-api-url") || scriptApiUrl || this.hostOrigin;

    // Cloud vs Box data-api-url bootstrap hint (core/channels.md §5.2). The
    // authoritative deployment_mode is returned by /api/v1/widget/config and is
    // reconciled in initSession(), so the server always wins over this hint.
    if (this.isBoxMode) {
      this.apiUrl = rawApiUrl;
    } else {
      this.apiUrl = this.hostOrigin;
    }

    this.render();

    if (!this.slug) {
      this.addMessage("bot", "Error: Missing required data-tenant-slug attribute.");
      return;
    }

    this.initSession();
  }

  private render(): void {
    this.shadow.innerHTML = `
      <style>
        :host {
          --rag-primary: #0066ff;
          --rag-bg: #ffffff;
          --rag-text: #1a1a1a;
          --rag-border: #e2e8f0;
          --rag-msg-bot: #f1f5f9;
          --rag-msg-user: #0066ff;
          --rag-msg-user-text: #ffffff;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          position: fixed;
          bottom: 20px;
          right: 20px;
          z-index: 999999;
        }

        .rag-launcher {
          width: 56px;
          height: 56px;
          border-radius: 28px;
          background: var(--rag-primary);
          color: #ffffff;
          display: flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
          transition: transform 0.2s ease, box-shadow 0.2s ease;
          border: none;
          font-size: 24px;
        }

        .rag-launcher:hover {
          transform: scale(1.05);
          box-shadow: 0 6px 16px rgba(0, 0, 0, 0.2);
        }

        .rag-window {
          position: fixed;
          bottom: 90px;
          right: 20px;
          width: 380px;
          height: 580px;
          max-width: calc(100vw - 40px);
          max-height: calc(100vh - 120px);
          background: var(--rag-bg);
          border-radius: 16px;
          box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
          display: none;
          flex-direction: column;
          overflow: hidden;
          border: 1px solid var(--rag-border);
        }

        .rag-window.open {
          display: flex;
        }

        .rag-header {
          padding: 16px;
          background: var(--rag-primary);
          color: #ffffff;
          display: flex;
          align-items: center;
          justify-content: space-between;
        }

        .rag-header-content {
          display: flex;
          flex-direction: column;
        }

        .rag-title {
          font-size: 16px;
          font-weight: 600;
          margin: 0;
        }

        .rag-subtitle {
          font-size: 12px;
          opacity: 0.85;
          margin-top: 2px;
        }

        .rag-close-btn {
          background: transparent;
          border: none;
          color: #ffffff;
          font-size: 20px;
          cursor: pointer;
          opacity: 0.8;
          padding: 4px;
        }

        .rag-close-btn:hover {
          opacity: 1;
        }

        .rag-messages {
          flex: 1;
          padding: 16px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
          gap: 12px;
        }

        .rag-msg {
          max-width: 85%;
          padding: 10px 14px;
          border-radius: 12px;
          font-size: 14px;
          line-height: 1.45;
          word-break: break-word;
        }

        .rag-msg-bot {
          background: var(--rag-msg-bot);
          color: var(--rag-text);
          align-self: flex-start;
          border-bottom-left-radius: 4px;
        }

        .rag-msg-user {
          background: var(--rag-msg-user);
          color: var(--rag-msg-user-text);
          align-self: flex-end;
          border-bottom-right-radius: 4px;
        }

        .rag-pills {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          margin-top: 8px;
        }

        .rag-pill {
          background: #ffffff;
          border: 1px solid var(--rag-primary);
          color: var(--rag-primary);
          padding: 6px 10px;
          border-radius: 14px;
          font-size: 12px;
          cursor: pointer;
          transition: background 0.15s ease;
        }

        .rag-pill:hover {
          background: #f0f7ff;
        }

        .rag-citations {
          font-size: 11px;
          margin-top: 8px;
          border-top: 1px solid #cbd5e1;
          padding-top: 6px;
          color: #475569;
        }

        .rag-feedback {
          display: flex;
          gap: 8px;
          margin-top: 6px;
        }

        .rag-feedback-btn {
          background: transparent;
          border: 1px solid #cbd5e1;
          border-radius: 6px;
          padding: 2px 8px;
          cursor: pointer;
          font-size: 12px;
        }

        .rag-feedback-btn:hover {
          background: #e2e8f0;
        }

        .rag-input-box {
          padding: 12px;
          border-top: 1px solid var(--rag-border);
          display: flex;
          gap: 8px;
          background: #ffffff;
        }

        .rag-input {
          flex: 1;
          border: 1px solid var(--rag-border);
          border-radius: 20px;
          padding: 8px 14px;
          font-size: 14px;
          outline: none;
        }

        .rag-input:focus {
          border-color: var(--rag-primary);
        }

        .rag-send-btn {
          background: var(--rag-primary);
          color: #ffffff;
          border: none;
          border-radius: 20px;
          padding: 8px 16px;
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
        }

        .rag-send-btn:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
      </style>

      <div class="rag-window" id="rag-window">
        <div class="rag-header">
          <div class="rag-header-content">
            <div class="rag-title" id="rag-title">Enterprise Assistant</div>
            <div class="rag-subtitle" id="rag-subtitle">Corporate RAG Search</div>
          </div>
          <button class="rag-close-btn" id="rag-close-btn">✕</button>
        </div>
        <div class="rag-messages" id="rag-messages"></div>
        <div class="rag-input-box">
          <input type="text" class="rag-input" id="rag-input" placeholder="Type your question..." />
          <button class="rag-send-btn" id="rag-send-btn">Send</button>
        </div>
      </div>

      <button class="rag-launcher" id="rag-launcher">💬</button>
    `;

    const launcher = this.shadow.getElementById("rag-launcher") as HTMLButtonElement;
    const closeBtn = this.shadow.getElementById("rag-close-btn") as HTMLButtonElement;
    const windowEl = this.shadow.getElementById("rag-window") as HTMLElement;
    this.messagesContainer = this.shadow.getElementById("rag-messages") as HTMLElement;
    this.inputField = this.shadow.getElementById("rag-input") as HTMLInputElement;
    const sendBtn = this.shadow.getElementById("rag-send-btn") as HTMLButtonElement;

    launcher.addEventListener("click", () => {
      this.isOpen = !this.isOpen;
      windowEl.classList.toggle("open", this.isOpen);
      if (this.isOpen) {
        this.inputField.focus();
      }
    });

    closeBtn.addEventListener("click", () => {
      this.isOpen = false;
      windowEl.classList.remove("open");
    });

    sendBtn.addEventListener("click", () => this.handleSendMessage());
    this.inputField.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        this.handleSendMessage();
      }
    });
  }

  private async initSession(): Promise<void> {
    try {
      // 1. Fetch Branding Config
      const configRes = await fetch(`${this.apiUrl}/api/v1/widget/config?slug=${encodeURIComponent(this.slug)}`);
      if (configRes.ok) {
        const config = await configRes.json();
        this.config = config;

        // The server is authoritative about Cloud vs Box (core/channels.md §5.2):
        // in saas_cloud any data-api-url override must be ignored.
        if (config.deployment_mode === "saas_cloud" && this.apiUrl !== this.hostOrigin) {
          console.warn("RAG widget: ignoring data-api-url override in saas_cloud mode");
          this.apiUrl = this.hostOrigin;
        }

        this.applyBranding();
      }

      // 2. Handshake session token
      const sessionRes = await fetch(`${this.apiUrl}/api/v1/widget/session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: this.slug, auth_mode: "anonymous" }),
      });

      if (sessionRes.ok) {
        const sessionData = await sessionRes.json();
        this.sessionToken = sessionData.session_token;
      } else {
        this.addMessage("bot", "Error initializing widget session. Please check allowed origins.");
      }
    } catch {
      this.addMessage("bot", "Cannot connect to RAG server.");
    }
  }

  private applyBranding(): void {
    if (!this.config) return;
    const { branding } = this.config;

    if (branding.title) {
      const titleEl = this.shadow.getElementById("rag-title");
      if (titleEl) titleEl.textContent = branding.title;
    }

    if (branding.subtitle) {
      const subEl = this.shadow.getElementById("rag-subtitle");
      if (subEl) subEl.textContent = branding.subtitle;
    }

    if (branding.primary_color) {
      this.style.setProperty("--rag-primary", branding.primary_color);
      this.style.setProperty("--rag-msg-user", branding.primary_color);
    }

    if (branding.welcome_message) {
      this.addMessage("bot", branding.welcome_message, undefined, branding.suggested_questions);
    }
  }

  private addMessage(
    sender: "bot" | "user",
    text: string,
    citations?: Citation[],
    suggestedQuestions?: string[],
    traceId?: string
  ): HTMLElement {
    const msgEl = document.createElement("div");
    msgEl.className = `rag-msg rag-msg-${sender}`;
    msgEl.textContent = text;
    if (traceId) {
      msgEl.setAttribute("data-trace-id", traceId);
    }

    if (citations && citations.length > 0) {
      const citEl = document.createElement("div");
      citEl.className = "rag-citations";
      citEl.textContent = "Sources: " + citations.map((c) => `[${c.citation_index}] ${c.document_title}`).join(", ");
      msgEl.appendChild(citEl);
    }

    if (sender === "bot" && this.config?.capabilities.supports_feedback_buttons) {
      const fbContainer = document.createElement("div");
      fbContainer.className = "rag-feedback";
      fbContainer.innerHTML = `
        <button class="rag-feedback-btn" data-action="up" title="Helpful">👍</button>
        <button class="rag-feedback-btn" data-action="down" title="Not helpful">👎</button>
      `;
      fbContainer.querySelectorAll<HTMLButtonElement>(".rag-feedback-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.getAttribute("data-action");
          fbContainer.innerHTML = `<span style="font-size:11px;color:#64748b;">Feedback sent (${action === "up" ? "👍" : "👎"})</span>`;
        });
      });
      msgEl.appendChild(fbContainer);
    }

    if (suggestedQuestions && suggestedQuestions.length > 0) {
      const pillsContainer = document.createElement("div");
      pillsContainer.className = "rag-pills";
      suggestedQuestions.forEach((q) => {
        const pill = document.createElement("button");
        pill.className = "rag-pill";
        pill.textContent = q;
        pill.addEventListener("click", () => {
          this.inputField.value = q;
          this.handleSendMessage();
        });
        pillsContainer.appendChild(pill);
      });
      msgEl.appendChild(pillsContainer);
    }

    this.messagesContainer.appendChild(msgEl);
    this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    return msgEl;
  }

  private async handleSendMessage(): Promise<void> {
    const text = this.inputField.value.trim();
    if (!text || !this.sessionToken) return;

    this.inputField.value = "";
    this.addMessage("user", text);

    const botMsgEl = this.addMessage("bot", "Thinking...");

    try {
      const response = await fetch(`${this.apiUrl}/api/v1/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.sessionToken}`,
        },
        body: JSON.stringify({ query: text }),
      });

      if (!response.ok) {
        botMsgEl.textContent = `Error: ${response.statusText}`;
        return;
      }

      botMsgEl.textContent = "";
      const reader = response.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let accumulatedText = "";
      let parsedCitations: Citation[] = [];
      let traceId = "";
      let currentEvent = "message";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split("\n");

        for (const line of lines) {
          if (line.startsWith("event: ")) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            try {
              const data = JSON.parse(line.slice(6));
              if (currentEvent === "token" && data.token) {
                accumulatedText += data.token;
                botMsgEl.textContent = accumulatedText;
              } else if (currentEvent === "citations" && Array.isArray(data.citations)) {
                parsedCitations = data.citations;
              } else if (currentEvent === "done") {
                traceId = data.trace_id || "";
              } else if (currentEvent === "error" && data.error) {
                botMsgEl.textContent = `Error: ${data.error}`;
              }
            } catch {
              // Ignore non-json SSE lines
            }
          }
        }
      }

      // Re-render finalized message with citations and feedback controls
      botMsgEl.textContent = accumulatedText || botMsgEl.textContent;
      if (traceId) {
        botMsgEl.setAttribute("data-trace-id", traceId);
      }
      if (parsedCitations.length > 0 && this.config?.capabilities.supports_citations) {
        const citEl = document.createElement("div");
        citEl.className = "rag-citations";
        citEl.textContent = "Sources: " + parsedCitations.map((c) => `[${c.citation_index}] ${c.document_title}`).join(", ");
        botMsgEl.appendChild(citEl);
      }

      if (this.config?.capabilities.supports_feedback_buttons) {
        const fbContainer = document.createElement("div");
        fbContainer.className = "rag-feedback";
        fbContainer.innerHTML = `
          <button class="rag-feedback-btn" data-action="up" title="Helpful">👍</button>
          <button class="rag-feedback-btn" data-action="down" title="Not helpful">👎</button>
        `;
        fbContainer.querySelectorAll<HTMLButtonElement>(".rag-feedback-btn").forEach((btn) => {
          btn.addEventListener("click", () => {
            const action = btn.getAttribute("data-action");
            fbContainer.innerHTML = `<span style="font-size:11px;color:#64748b;">Feedback sent (${action === "up" ? "👍" : "👎"})</span>`;
          });
        });
        botMsgEl.appendChild(fbContainer);
      }
      this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    } catch (e) {
      botMsgEl.textContent = `Connection error: ${e}`;
    }
  }
}

if (!customElements.get("rag-widget")) {
  customElements.define("rag-widget", RagWidgetElement);
}

// Auto-mount if script is loaded on page
function autoBootstrap(): void {
  const existing = document.querySelector("rag-widget");
  if (!existing) {
    const widget = document.createElement("rag-widget");
    document.body.appendChild(widget);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", autoBootstrap);
} else {
  autoBootstrap();
}

export { RagWidgetElement };
