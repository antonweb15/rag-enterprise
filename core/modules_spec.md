# Python Modules & Contract Interfaces Specification (`core/modules_spec.md`)

## 1. Базовые сущности и Pydantic V2 модели

```python
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Перечисления ---
class DocumentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"

class AccessLevel(int, Enum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    STRICT = 3

class IntentType(str, Enum):
    RAG_SEARCH = "rag_search"
    CHIT_CHAT = "chit_chat"
    META_COMMAND = "meta_command"

class AdminRole(str, Enum):
    ADMIN = "admin"                            # полный доступ: документы, лицензия, пользователи
    OPERATOR = "operator"                      # загрузка и переиндексация без удаления
    VIEWER = "viewer"                          # только чтение списков, чанков и дашборда

class DeploymentMode(str, Enum):
    SAAS_CLOUD = "saas_cloud"
    ON_PREMISE_BOX = "on_premise_box"

# --- Модели лицензирования и тенантов ---
class LicensePayload(BaseModel):
    license_id: str
    client_id: str
    plan: str = "enterprise_box"
    mode: str = "online_subscription"          # 'online_subscription' | 'airgap_enterprise'
    issued_at: datetime
    expires_at: Optional[datetime] = None      # None для бессрочных On-Premise лицензий
    max_documents: int = 1000
    max_users: int = 10
    hardware_id: Optional[str] = None
    telemetry_opt_out: bool = True
    features: List[str] = Field(default_factory=lambda: ["hybrid_search", "s3_connector", "flashrank_rerank"])

class TenantContext(BaseModel):
    tenant_id: UUID
    slug: str
    name: str
    is_active: bool = True
    storage_type: str = "s3"
    s3_bucket: Optional[str] = None
    s3_prefix: Optional[str] = None
    allowed_origins: List[str] = Field(default_factory=list) # Строгий список доменов без wildcard

class WebWidgetSessionRequest(BaseModel):
    slug: str

class WebWidgetSessionResponse(BaseModel):
    session_token: str
    tenant_id: UUID
    expires_in: int = 3600

# --- Модели пользователей и контекста ---
class UserContext(BaseModel):
    user_id: UUID                      # Обязательный ID из таблицы users (без fallback uuid4)
    tenant_id: UUID                    # ID привязанного тенанта
    channel_user_id: str
    channel: str = "telegram"
    access_level: AccessLevel = AccessLevel.PUBLIC

# --- Модели документов и чанков ---
class RawDocument(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    source_type: str = "s3"
    source_uri: str
    content_hash: str
    language: str = "ru"
    access_level: AccessLevel = AccessLevel.PUBLIC
    raw_content: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ParentChunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    document_id: UUID
    content: str
    token_count: int
    chunk_index: int
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ChildChunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    parent_id: UUID
    content: str
    token_count: int
    chunk_index: int
    access_level: AccessLevel = AccessLevel.PUBLIC
    dense_embedding: Optional[List[float]] = None
    sparse_embedding: Optional[Dict[int, float]] = None
    embedding_model_id: str = "BAAI/bge-m3"
    embedding_version: int = 1

# --- Модели поиска и генерации ---
class RetrievalCandidate(BaseModel):
    child_id: UUID
    parent_id: UUID
    document_id: UUID
    content: str
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None
    rrf_score: float
    rerank_score: Optional[float] = None

class GenerationCitation(BaseModel):
    citation_index: int                # Порядковый номер сноски в тексте ответа [1], [2]
    chunk_id: UUID                     # Реальный UUID чанка в child_chunks
    document_title: str
    source_uri: str
    page: Optional[int] = None

class RAGResponse(BaseModel):
    answer: str
    citations: List[GenerationCitation] = Field(default_factory=list)
    confidence_passed: bool = True
    trace_id: Optional[str] = None

# --- Модели админ-панели (см. core/admin_panel.md) ---
class AdminConfig(BaseModel):
    """Runtime-конфиг для единого билда SPA: различия режимов без форков."""
    deployment_mode: DeploymentMode
    features: List[str] = Field(default_factory=list)   # 'tenant_management', 'license_activation', 'local_file_manager', ...
    branding: Dict[str, Any] = Field(default_factory=dict)
    api_version: str = "v1"

class DocumentUploadRequest(BaseModel):
    tenant_id: UUID
    file_name: str
    content_hash: str                                   # sha256, рассчитан на клиенте для ранней дедупликации
    size_bytes: int
    access_level: AccessLevel = AccessLevel.PUBLIC
    relative_path: Optional[str] = None                 # путь внутри загруженной папки (webkitdirectory)

class DocumentListItem(BaseModel):
    id: UUID
    source_uri: str
    file_name: str
    status: DocumentStatus
    access_level: AccessLevel = AccessLevel.PUBLIC
    size_bytes: Optional[int] = None
    parent_chunk_count: int = 0
    child_chunk_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class ChunkView(BaseModel):
    """Эмбеддинги НЕ отдаются по умолчанию: 1024 float x N чанков = мегабайты JSON.
    dense_embedding/sparse_embedding заполняются только при include_vectors=true и только для одного чанка."""
    chunk_id: UUID
    parent_id: Optional[UUID] = None
    level: Literal["parent", "child"]
    content: str
    token_count: int
    chunk_index: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
    dense_norm: Optional[float] = None
    has_sparse_vector: bool = False
    dense_embedding: Optional[List[float]] = None
    sparse_embedding: Optional[Dict[int, float]] = None

class LicenseActivationRequest(BaseModel):
    activation_code: Optional[str] = None               # 'RAG-ENT-XXXX-YYYY-ZZZZ'
    license_token: Optional[str] = None                 # PASETO v4.public / JWT (Ed25519)
    license_file_b64: Optional[str] = None              # содержимое загруженного .key файла

class MachineFingerprint(BaseModel):
    hardware_id: str                                    # sha256(cpu_id + mac_addr)
    hostname: str
    collected_at: datetime

class LicenseStatus(BaseModel):
    """Источник истины по лицензии — только бэкенд. Фронт отображает этот объект как есть."""
    is_valid: bool
    plan: str
    mode: str                                           # 'online_subscription' | 'airgap_enterprise'
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    max_documents: int = 0
    max_users: int = 0
    used_documents: int = 0
    used_users: int = 0
    hardware_id: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    grace_period_expires_at: Optional[datetime] = None
    restricted_mode: bool = False
    restricted_reason: Optional[str] = None

class AuditLogEntry(BaseModel):
    id: UUID
    tenant_id: Optional[UUID] = None
    actor_user_id: UUID
    actor_email: str
    action: str                                         # 'document.upload', 'document.delete', 'license.activate', ...
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

class IngestQueueItem(BaseModel):
    document_id: UUID
    file_name: str
    status: DocumentStatus
    stage: Optional[str] = None                         # 'parsing' | 'splitting' | 'embedding' | 'upserting'
    retry_count: int = 0
    error_message: Optional[str] = None
    updated_at: datetime

class DashboardStats(BaseModel):
    documents_total: int = 0
    documents_by_status: Dict[DocumentStatus, int] = Field(default_factory=dict)
    child_chunks_total: int = 0
    storage_bytes_used: int = 0
    queries_last_24h: int = 0
    p95_retrieval_latency_ms: Optional[float] = None

# --- Настройки приложения (pydantic-settings) ---
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Deployment & Licensing
    deployment_mode: DeploymentMode = DeploymentMode.SAAS_CLOUD
    license_key: Optional[str] = None
    license_public_key: Optional[str] = None
    license_server_url: Optional[str] = "https://license.your-rag-domain.com"

    # Supabase / Postgres
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[str] = None
    postgres_dsn: str = "postgresql://postgres:postgrespassword@localhost:5432/postgres"

    # S3 Object Storage (Cloudflare R2 / MinIO / Selectel / AWS)
    s3_endpoint_url: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_default_bucket: str = "enterprise-rag-documents"
    s3_region_name: str = "auto"

    # LLM & Embedder
    groq_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "BAAI/bge-m3"
    embedding_batch_size: int = 32

    # RAG Parameters
    retrieval_top_candidates: int = 30
    rrf_k: int = 60
    rrf_alpha: float = 0.6
    rrf_beta: float = 0.4
    confidence_threshold: float = 0.35
    max_history_turns: int = 5

    # Channels
    telegram_bot_token: Optional[str] = None

    # Admin Panel & Session Auth (см. core/admin_panel.md)
    admin_host: str = "127.0.0.1"                 # в режиме Box наружу порт не публикуется
    admin_port: int = 8080
    admin_static_dir: Optional[str] = "apps/admin/dist"   # отдача SPA как StaticFiles (один контейнер для Box)
    jwt_secret: Optional[str] = None              # HS256, per-install, >= 32 байт; генерируется при первом старте
    session_cookie_name: str = "rag_admin_session"
    session_ttl_seconds: int = 3600
    session_cookie_secure: bool = True            # HttpOnly + Secure + SameSite=Lax, НЕ localStorage
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_enabled: bool = True
    totp_required_for_admin: bool = True
    admin_rate_limit_per_minute: int = 120        # приватная политика (отдельно от публичного widget)
    widget_rate_limit_per_minute: int = 20        # публичная политика

    # Vector Store & Object Storage ports
    vector_store_provider: Literal["pgvector", "qdrant", "chroma"] = "pgvector"
    object_storage_provider: Literal["s3", "minio", "r2", "local"] = "s3"
    object_storage_local_root: str = "./data"

    # Observability
    langfuse_public_key: Optional[str] = None
    langfuse_secret_key: Optional[str] = None
    langfuse_host: str = "https://cloud.langfuse.com"
```

---

## 2. Абстрактные интерфейсы ядра

```python
from abc import ABC, abstractmethod

# --- 2.0. Licensing & Multi-Tenancy Interfaces ---
class BaseLicenseValidator(ABC):
    @abstractmethod
    def validate_token(self, token: str) -> LicensePayload:
        """Офлайн валидация цифровой подписи Ed25519 и срока действия токена."""
        pass

    @abstractmethod
    async def verify_heartbeat(self, license_payload: LicensePayload) -> bool:
        """Онлайн пинг на мастер-сервер лицензий для проверки отсутствия отзыва (с grace-периодом 72ч)."""
        pass

    @abstractmethod
    async def enforce_quota(self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]) -> None:
        """Проверка квот лицензии (max_documents, max_users) с выбросом QuotaExceededError при превышении."""
        pass

class BaseTenantResolver(ABC):
    @abstractmethod
    async def resolve_tenant(self, identifier: str) -> TenantContext:
        """Определение контекста тенанта по API-ключу, slug или домену виджета."""
        pass

    @abstractmethod
    async def enforce_quota(self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]) -> None:
        """Проверка текущего потребления ресурсов тенантом против установленных лимитов."""
        pass

# --- 2.0.1. Storage Ports (сменные реализации) ---
class VectorStoreCapabilities(BaseModel):
    supports_sparse: bool = True
    supports_hybrid_rrf: bool = False        # True только там, где есть нативная серверная реализация (PgVectorStore)
    max_dimensions: int = 1024

class BaseVectorStore(ABC):
    """Порт векторного хранилища. Контракт — раздельные плечи поиска;
    слияние RRF выполняется в application-layer (Retriever), а не внутри стора."""

    @property
    @abstractmethod
    def capabilities(self) -> VectorStoreCapabilities:
        """Capability-флаги конкретной реализации (pgvector / Qdrant / Chroma)."""
        pass

    @abstractmethod
    async def upsert_chunks(self, tenant_id: UUID, chunks: List[ChildChunk]) -> None:
        """Идемпотентная запись/обновление child-чанков вместе с векторами."""
        pass

    @abstractmethod
    async def search_dense(
        self, tenant_id: UUID, query_dense: List[float], k: int = 30, user_access_level: int = 0
    ) -> List[RetrievalCandidate]:
        """Плотное плечо: Top-k по косинусному расстоянию с фильтром tenant_id и access_level."""
        pass

    @abstractmethod
    async def search_sparse(
        self, tenant_id: UUID, query_sparse: Dict[int, float], k: int = 30, user_access_level: int = 0
    ) -> List[RetrievalCandidate]:
        """Лексическое плечо (sparsevec / BM25). Если supports_sparse=False — возвращает пустой список."""
        pass

    @abstractmethod
    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int:
        """Каскадное удаление векторов документа (для DELETE /admin/documents/{id})."""
        pass

class BaseObjectStorage(ABC):
    """Порт объектного хранилища (S3 / MinIO / R2 / local). Все операции строго per-tenant."""

    @abstractmethod
    async def put(self, tenant_id: UUID, key: str, data: bytes, content_type: Optional[str] = None) -> str:
        """Запись объекта по префиксу {tenant_id}/{key}. Возвращает storage_ref (URI)."""
        pass

    @abstractmethod
    async def get(self, tenant_id: UUID, key: str) -> bytes:
        pass

    @abstractmethod
    async def delete(self, tenant_id: UUID, key: str) -> None:
        pass

    @abstractmethod
    async def list(self, tenant_id: UUID, prefix: str = "", limit: int = 1000) -> List[str]:
        pass

# --- 2.1. Embedder Interface ---
class BaseEmbedder(ABC):
    @abstractmethod
    async def embed_documents(
        self, texts: List[str]
    ) -> Tuple[List[List[float]], List[Dict[int, float]]]:
        """Единый проход BGE-M3: генерация dense (1024-dim) и sparse представлений."""
        pass

    @abstractmethod
    async def embed_query(
        self, text: str
    ) -> Tuple[List[float], Dict[int, float]]:
        """Генерация dense и sparse вектора для поискового запроса."""
        pass

# --- 2.2. Query Rewriter & Intent Router ---
class BaseQueryRewriter(ABC):
    @abstractmethod
    async def rewrite_query(self, query: str, history: List[Dict[str, str]]) -> str:
        """Query Condensation: преобразование follow-up вопроса в самостоятельный запрос."""
        pass

class BaseIntentRouter(ABC):
    @abstractmethod
    async def route_intent(self, query: str) -> IntentType:
        """Маршрутизация намерения: RAG поиск, Chit-Chat или Meta команда."""
        pass

# --- 2.3. Retriever & Reranker Interface ---
class BaseRetriever(ABC):
    @abstractmethod
    async def retrieve(
        self, query: str, tenant_id: UUID, user_access_level: int = 0, top_k: int = 30
    ) -> List[RetrievalCandidate]:
        """Гибридный поиск Dense + Sparse с RRF слиянием и изоляцией по tenant_id."""
        pass

class Retriever(BaseRetriever):
    """Эталонная реализация: вызывает BaseVectorStore.search_dense() и .search_sparse(),
    затем выполняет RRF-слияние В КОДЕ (α=0.6, β=0.4, k=60).

    RRF — контракт application-layer, а не SQL. SQL-функция hybrid_search_rrf остаётся
    внутренней деталью оптимизации PgVectorStore (вызывается только при
    capabilities.supports_hybrid_rrf = True и идентична по результату коду-фьюжену).
    """

    def __init__(self, vector_store: BaseVectorStore, embedder: BaseEmbedder) -> None: ...

    @staticmethod
    def fuse_rrf(
        dense: List[RetrievalCandidate],
        sparse: List[RetrievalCandidate],
        k: int = 60,
        alpha: float = 0.6,
        beta: float = 0.4,
    ) -> List[RetrievalCandidate]:
        """RRF(d) = alpha/(k + rank_dense(d)) + beta/(k + rank_sparse(d)).
        Отсутствие документа в списке => ранг бесконечность => вклад слагаемого 0."""
        ...

class BaseReranker(ABC):
    @abstractmethod
    async def rerank(
        self, query: str, candidates: List[RetrievalCandidate]
    ) -> List[RetrievalCandidate]:
        """Переранжирование кандидатов (FlashRank) с вычислением rerank_score."""
        pass

class BaseConfidenceGate(ABC):
    @abstractmethod
    def evaluate(self, candidates: List[RetrievalCandidate], threshold: Optional[float] = None) -> bool:
        """Проверка max(rerank_score) >= threshold. Если threshold=None, используется settings.confidence_threshold."""
        pass

# --- 2.4. LLM Provider Interface ---
class BaseLLM(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        """Генерация полного текста или валидного JSON."""
        pass

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> AsyncGenerator[str, None]:
        """Потоковая генерация токенов (Token Stream)."""
        pass

# --- 2.5. Document Source Interface ---
class BaseDocumentSource(ABC):
    @abstractmethod
    async def fetch_documents(self) -> List[RawDocument]:
        """Загрузка документов из источника (локальная папка, S3, Confluence)."""
        pass

    @abstractmethod
    async def watch_for_changes(self) -> AsyncGenerator[RawDocument, None]:
        """Генератор событий добавления/изменения файлов с debounce."""
        pass

# --- 2.6. Channel Interface ---
class BaseChannel(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Запуск обработчика входящих сообщений канала."""
        pass

    @abstractmethod
    async def send_response(self, user_id: str, response: RAGResponse) -> None:
        """Отправка сформированного ответа пользователю."""
        pass

    @abstractmethod
    async def stream_response(
        self, user_id: str, token_stream: AsyncGenerator[str, None], initial_message_id: Optional[str] = None
    ) -> RAGResponse:
        """Потоковая передача ответа с троттлингом обновлений и сбором итогового RAGResponse."""
        pass
```

---

## 3. Фоновые задачи (Taskiq Tasks Contract)

```python
# src/tasks/ingestion_tasks.py
from taskiq import TaskiqDepends
from taskiq_aio_pika import AioPikaBroker  # Либо PostgresBroker / InMemoryBroker

# Инициализация брокера очереди
broker = AioPikaBroker()

@broker.task
async def process_document_task(
    tenant_id: UUID,
    storage_ref: str,                 # ссылка на объект в BaseObjectStorage ({tenant_id}/{key})
    content_hash: str,
    access_level: int = 0
) -> None:
    """
    1. Проверка активной квоты тенанта (enforce_quota(tenant_id, 'documents')).
    2. Проверка наличия content_hash в таблице documents для данного tenant_id (идемпотентность).
    3. Чтение файла через BaseObjectStorage.get(tenant_id, key) и парсинг структуры (Docling / layout parser).
    4. Token-aware нарезка на Parent и Child чанки с установкой tenant_id и access_level.
    5. Генерация батчей BGE-M3 эмбеддингов (batch_size=32).
    6. Транзакционная запись (BaseVectorStore.upsert_chunks) и перевод статуса в 'indexed'.
    7. Публикация события прогресса: NOTIFY ingest_events (для SSE админки). In-memory pub/sub запрещён.
    """
    pass
```

---

## 4. Контракт безопасности сессий админки (`fastapi-users`)

```python
# services/api/admin/auth.py
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy

# HTTP-only cookie: JWT НИКОГДА не попадает в JS-контекст (no localStorage / sessionStorage)
cookie_transport = CookieTransport(
    cookie_name=settings.session_cookie_name,
    cookie_max_age=settings.session_ttl_seconds,
    cookie_path="/api/v1",
    cookie_httponly=True,      # обязательно
    cookie_secure=True,        # обязательно (HTTPS / SSH-туннель)
    cookie_samesite="lax",     # обязательно
)

def get_jwt_strategy() -> JWTStrategy:
    # HS256 + per-install секрет (>= 32 байт), генерируется при первом старте и ротируется при деплое.
    # ЗАПРЕЩЕНО использовать здесь LICENSE_SIGNING_KEY (Ed25519, вендорский офлайн-ключ):
    # компрометация инстанса клиента дала бы возможность выписывать лицензии от имени вендора.
    return JWTStrategy(secret=settings.jwt_secret, lifetime_seconds=settings.session_ttl_seconds)

admin_auth_backend = AuthenticationBackend(
    name="admin_cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)
```

**Обязательные правила:**
- ⛔ `localStorage` / `sessionStorage` для токенов — **запрещены**; SPA не передаёт `Authorization: Bearer`.
- CSRF-защита (`X-CSRF-Token` / double-submit cookie) обязательна для `POST`, `PUT`, `PATCH`, `DELETE`.
- TOTP 2FA обязателен для роли `AdminRole.ADMIN`.
- Каждая мутация пишет `AuditLogEntry` (actor, IP, User-Agent) в той же транзакции.
- Детали см. в [`admin_panel.md`](./admin_panel.md) §4.
