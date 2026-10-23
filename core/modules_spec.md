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

# --- Модели пользователей и контекста ---
class UserContext(BaseModel):
    user_id: UUID                      # Обязательный ID из таблицы users (без fallback uuid4)
    channel_user_id: str
    channel: str = "telegram"
    access_level: AccessLevel = AccessLevel.PUBLIC

# --- Модели документов и чанков ---
class RawDocument(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_type: str = "local_folder"
    source_uri: str
    content_hash: str
    language: str = "ru"
    access_level: AccessLevel = AccessLevel.PUBLIC
    raw_content: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ParentChunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    content: str
    token_count: int
    chunk_index: int
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ChildChunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    parent_id: UUID
    content: str
    token_count: int
    chunk_index: int
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

    # Supabase / Postgres
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[str] = None
    postgres_dsn: str = "postgresql://postgres:postgrespassword@localhost:5432/postgres"

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
        self, query: str, user_access_level: int = 0, top_k: int = 30
    ) -> List[RetrievalCandidate]:
        """Гибридный поиск Dense + Sparse с RRF слиянием на уровне базы данных."""
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
    document_uri: str,
    content_hash: str,
    access_level: int = 0
) -> None:
    """
    1. Проверка наличия document_hash в Supabase (пропуск если уже indexed).
    2. Парсинг структуры (Docling / layout parser).
    3. Token-aware нарезка на Parent и Child чанки.
    4. Генерация батчей BGE-M3 эмбеддингов (batch_size=32).
    5. Транзакционная запись в БД и перевод статуса в 'indexed'.
    """
    pass
```
