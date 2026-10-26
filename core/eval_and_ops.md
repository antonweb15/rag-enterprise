# Evaluation, Observability & DevOps Specification (`core/eval_and_ops.md`)

## 1. Регламент оценки качества (RAGAS & Golden Dataset)

### 1.1. Золотой верификационный датасет (`tests/data/golden_dataset.json`)
Формат тестовых примеров (BEIR / QRELS совместимый):
```json
[
  {
    "id": "eval_001",
    "question": "Каков максимальный размер родительского чанка в системе?",
    "ground_truth_answer": "Размер родительского чанка составляет от 800 до 1200 токенов.",
    "ground_truth_contexts": [
      "Parent Chunks: 800–1200 токенов (логические секции с метаданными заголовков и структуры)."
    ],
    "ground_truth_chunk_ids": [
      "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    ],
    "access_level": 0
  }
]
```

### 1.2. Целевые метрики RAGAS & IR SLO
- **Retrieval Metrics (на основе `ground_truth_chunk_ids`)**:
  - `Hit Rate@5` $\ge 0.85$ (процент случаев, когда эталонный чанк вошел в Top-5).
  - `MRR@5` (Mean Reciprocal Rank) $\ge 0.75$.
  - `nDCG@5` $\ge 0.80$.
- **Generation & Alignment Metrics (RAGAS)**:
  - `Faithfulness` $\ge 0.90$ (отсутствие неподтвержденных галлюцинаций).
  - `Answer Relevance` $\ge 0.85$ (точность ответа на поставленный вопрос).
  - `Context Precision` $\ge 0.85$ (ранжирование релевантных контекстов выше шумовых).
  - `Context Recall` $\ge 0.85$ (полнота извлечения всей необходимой для ответа информации).

---

## 2. Наблюдаемость и Трейсинг (Langfuse Observability)

- **Провайдер**: Langfuse Cloud (Free Tier) или Self-Hosted.
- Каждый входящий запрос формирует `Trace` в Langfuse с деревом спанов:
  1. `Span: intent_routing` (вход, intent, score, latency).
  2. `Span: query_condensation` (chat_history, raw_query, standalone_query).
  3. `Span: hybrid_retrieval` (dense_time, sparse_time, rrf_top30_chunks).
  4. `Span: reranking` (flashrank_scores, confidence_gate_passed).
  5. `Generation: llm_call` (prompt_tokens, completion_tokens, total_cost, model_id).
- **Online Feedback**: Нажатие кнопок 👍/👎 в Telegram обновляет trace в Langfuse по `trace_id` со значением score: `+1` / `-1`.

---

## 3. Контейнеризация и Docker (Multi-stage)

### 3.1. `Dockerfile`
```dockerfile
# Stage 1: Build & Dependencies
FROM python:3.11-slim as builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final Runtime
FROM python:3.11-slim as runner

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

COPY core/ core/
COPY src/ src/

# Default CMD (override in docker-compose for worker)
CMD ["python", "-m", "src.main_bot"]
```

### 3.2. `docker-compose.yml`
```yaml
services:
  telegram-bot:
    build:
      context: .
      dockerfile: Dockerfile
    command: python -m src.main_bot
    env_file: .env
    restart: unless-stopped
    depends_on:
      - postgres-pgvector

  ingestion-worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: taskiq worker src.tasks.ingestion_tasks:broker
    env_file: .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    depends_on:
      - postgres-pgvector

  postgres-pgvector:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgrespassword
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

---

## 4. CI/CD Пайплайн (GitHub Actions & Protected Box Distribution)

Workflow-файл `.github/workflows/ci.yml`:
1. **Lint & Static Check**: `ruff check .` и `mypy src` (строгая проверка без проглатывания ошибок).
2. **Unit & Integration Tests**: `pytest tests/ -v` (проверка изоляции тенантов, валидации токенов Ed25519, RRF и S3 коннектора).
3. **Evaluation Gate**: Запуск `python -m tests.eval_ragas` — проверка, что метрики качества на `golden_dataset.json` не деградировали ниже допустимого порога SLO.
4. **On-Premise Core Protection & Image Release** (при тегах `v*`):
   - Компиляция модулей ядра (`rag_core/licensing/`, `rag_core/retrieval/`) в `.so` через Cython/PyArmor.
   - Сборка закрытого Docker-образа `rag-core:enterprise-vX.Y`.
   - Публикация в приватный GitHub Container Registry (`ghcr.io/your-org/rag-core`).
5. **Secrets**: Управление конфиденциальными переменными (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `LICENSE_PRIVATE_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`) строго через **GitHub Actions Secrets**.
