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
    document_uri: str,
    content_hash: str,
    access_level: int = 0
) -> None:
    """
    1. Проверка активной квоты тенанта (enforce_quota(tenant_id, 'documents')).
    2. Проверка наличия content_hash в таблице documents для данного tenant_id (идемпотентность).
    3. Парсинг структуры (Docling / layout parser).
    4. Token-aware нарезка на Parent и Child чанки с установкой tenant_id и access_level.
    5. Генерация батчей BGE-M3 эмбеддингов (batch_size=32).
    6. Транзакционная запись в БД и перевод статуса в 'indexed'.
    """
    pass
```
