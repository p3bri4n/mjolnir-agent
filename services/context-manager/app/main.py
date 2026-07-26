"""
Context Manager: retrieval (RAG) and long-term memory, stored in Qdrant
(two distinct collections: "documents" and "memory").

NOTE: this skeleton uses sentence-transformers for embeddings (model
downloaded from Hugging Face on first startup -> network access
required). Replace with a local model if the deployment must be 100%
air-gapped. In test environments, EMBEDDING_MODEL=fake switches to a
deterministic embedder with no network or sentence-transformers
dependency (see tests/conftest.py).

The LlamaIndex / Mem0 / LLMLingua / reranker building blocks mentioned in
the architecture can be plugged in here: LlamaIndex for advanced
chunking/ingestion, Mem0 for per-user structured memory, LLMLingua to
compress context before sending it to the LLM, a cross-encoder to
rerank results before returning them to the agent.
"""

import os
import time
import uuid

from fastapi import FastAPI
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

class _DeterministicFakeEmbedder:
    """
    Embedder with no network dependency, enabled via EMBEDDING_MODEL=fake.
    Used only by the test suite: it produces no semantically valid
    embedding, only a deterministic vector based on a hash of the text,
    enough to exercise Qdrant's logic (upsert/query) without downloading
    a model from Hugging Face.
    """

    def __init__(self, dim: int = 384):
        self._dim = dim

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(self, text: str):
        import hashlib
        import numpy as np

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        repeated = (digest * (self._dim // len(digest) + 1))[: self._dim]
        return np.array([b / 255.0 for b in repeated])


app = FastAPI(title="Context Manager")

# ":memory:" lets the tests run with no real Qdrant instance;
# in production, QDRANT_URL always points to the compose's qdrant container.
qdrant = QdrantClient(location=":memory:") if QDRANT_URL == ":memory:" else QdrantClient(url=QDRANT_URL)

def _build_embedder():
    if EMBEDDING_MODEL_NAME == "fake":
        return _DeterministicFakeEmbedder()
    # imported here only: avoids depending on sentence-transformers/torch
    # when EMBEDDING_MODEL=fake (test mode, see tests/conftest.py)
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


embedder = _build_embedder()
VECTOR_SIZE = embedder.get_sentence_embedding_dimension()


def _ensure_collections(max_retries: int = 10, delay_seconds: float = 3.0):
    """
    Waits for Qdrant to be reachable before creating the collections.
    `depends_on` in docker-compose only guarantees that the Qdrant
    CONTAINER has started, not that it already accepts connections:
    without this retry, a startup race crashes this service on the first
    `docker compose up`.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            for collection in ("documents", "memory"):
                if not qdrant.collection_exists(collection):
                    qdrant.create_collection(
                        collection_name=collection,
                        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
                    )
            return
        except Exception as exc:  # noqa: BLE001 - retry on any network error
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"Unable to reach Qdrant after {max_retries} attempts") from last_error


_ensure_collections()


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 5
    collection: str = "documents"


class IngestRequest(BaseModel):
    text: str
    metadata: dict = {}
    collection: str = "documents"


class RememberRequest(BaseModel):
    text: str
    user_id: str = "default"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/retrieve")
async def retrieve(request: RetrieveRequest):
    vector = embedder.encode(request.query).tolist()
    hits = qdrant.query_points(
        collection_name=request.collection, query=vector, limit=request.top_k
    ).points
    return {"results": [hit.payload.get("text", "") for hit in hits]}


@app.post("/ingest")
async def ingest(request: IngestRequest):
    vector = embedder.encode(request.text).tolist()
    point_id = str(uuid.uuid4())
    qdrant.upsert(
        collection_name=request.collection,
        points=[
            PointStruct(
                id=point_id,
                vector=vector,
                payload={"text": request.text, **request.metadata},
            )
        ],
    )
    return {"id": point_id}


@app.post("/remember")
async def remember(request: RememberRequest):
    """Stores a long-term memory fact tied to a user."""
    vector = embedder.encode(request.text).tolist()
    point_id = str(uuid.uuid4())
    qdrant.upsert(
        collection_name="memory",
        points=[
            PointStruct(
                id=point_id,
                vector=vector,
                payload={"text": request.text, "user_id": request.user_id},
            )
        ],
    )
    return {"id": point_id}
