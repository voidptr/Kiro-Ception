"""Embedding backend abstraction supporting sentence-transformers and OpenAI-compatible APIs."""

import logging
from abc import ABC, abstractmethod

import numpy as np

from .config import get_config

logger = logging.getLogger(__name__)


class EmbeddingBackend(ABC):
    """Abstract base class for embedding backends."""

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode a list of texts into normalized embedding vectors.

        Args:
            texts: List of strings to embed.

        Returns:
            numpy array of shape (len(texts), dimensions) with L2-normalized vectors.
        """
        ...

    @abstractmethod
    def encode_query(self, query: str) -> np.ndarray:
        """Encode a single query string into a normalized embedding vector.

        Args:
            query: The search query.

        Returns:
            numpy array of shape (dimensions,) with L2-normalized vector.
        """
        ...

    @abstractmethod
    def dimensions(self) -> int:
        """Return the embedding dimensionality."""
        ...

    @abstractmethod
    def fingerprint(self) -> str:
        """Return a string fingerprint identifying this backend configuration.

        Used to detect config changes that require cache invalidation.
        """
        ...


class SentenceTransformersBackend(EmbeddingBackend):
    """Backend using the sentence-transformers library (local, no network after model download)."""

    def __init__(self):
        self._model = None
        self._config = get_config()

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            model_name = self._config.embedding.model
            print(f"[kiro-ception] Loading embedding model '{model_name}'...")
            try:
                # After the first run the model is already in the local HF
                # cache, but SentenceTransformer still revalidates against the
                # Hub on every load — several network round-trips costing ~6s
                # per engine start, and a hard dependency on being online.
                # Load straight from cache and only reach out if it is absent.
                self._model = SentenceTransformer(model_name, local_files_only=True)
            except Exception:
                print(
                    f"[kiro-ception] '{model_name}' not in local cache — "
                    f"downloading (first run only)..."
                )
                self._model = SentenceTransformer(model_name)
            print("[kiro-ception] Model loaded.")
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        model = self._get_model()
        return model.encode(
            texts,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

    def encode_query(self, query: str) -> np.ndarray:
        model = self._get_model()
        return model.encode(
            query,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

    def dimensions(self) -> int:
        return self._get_model().get_sentence_embedding_dimension()

    def fingerprint(self) -> str:
        return f"sentence-transformers:{self._config.embedding.model}"


class OpenAICompatibleBackend(EmbeddingBackend):
    """Backend using any OpenAI-compatible embeddings API (Ollama, LM Studio, OpenAI, etc.).

    Communicates via HTTP to a local or remote endpoint. Supports optional API key
    for hosted providers and configurable output dimensions.
    """

    def __init__(self):
        self._config = get_config()
        self._api_base = self._config.embedding.api_base
        self._api_key = self._config.embedding.api_key
        self._model_name = self._config.embedding.model
        self._dimensions = self._config.embedding.dimensions
        self._session = None

    def _get_session(self):
        """Lazy-create a requests session for connection pooling."""
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers["Content-Type"] = "application/json"
            if self._api_key:
                self._session.headers["Authorization"] = f"Bearer {self._api_key}"
        return self._session

    def _embed(self, input_data: str | list[str]) -> np.ndarray:
        """Call the embeddings endpoint."""
        session = self._get_session()

        # Build request body
        body: dict = {
            "model": self._model_name,
            "input": input_data,
        }
        if self._dimensions:
            body["dimensions"] = self._dimensions

        url = f"{self._api_base.rstrip('/')}/embeddings"
        response = session.post(url, json=body, timeout=600)
        response.raise_for_status()

        data = response.json()
        embeddings = [item["embedding"] for item in data["data"]]
        result = np.array(embeddings, dtype=np.float32)

        # Normalize vectors (L2 normalization)
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        result = result / norms

        return result

    def encode(self, texts: list[str]) -> np.ndarray:
        # Use configured batch size for API calls
        config = get_config()
        batch_size = config.embedding.batch_size or 16
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_result = self._embed(batch)
            all_embeddings.append(batch_result)

        return np.vstack(all_embeddings) if all_embeddings else np.array([])

    def encode_query(self, query: str) -> np.ndarray:
        result = self._embed(query)
        return result[0]

    def dimensions(self) -> int:
        if self._dimensions:
            return self._dimensions
        # Probe the API with a short text to discover dimensions
        result = self._embed("hello")
        return result.shape[1]

    def fingerprint(self) -> str:
        dim_part = f":{self._dimensions}" if self._dimensions else ""
        return f"openai-compatible:{self._api_base}:{self._model_name}{dim_part}"


def get_embedding_backend() -> EmbeddingBackend:
    """Create the appropriate embedding backend based on configuration."""
    config = get_config()
    backend_type = config.embedding.backend

    if backend_type == "openai-compatible":
        if not config.embedding.api_base:
            raise ValueError(
                "embedding.api_base is required when backend = 'openai-compatible'. "
                "Set it to your Ollama, LM Studio, or OpenAI endpoint "
                "(e.g., 'http://localhost:11434/v1' for Ollama)."
            )
        print(
            f"[kiro-ception] Using OpenAI-compatible backend: "
            f"{config.embedding.api_base} model={config.embedding.model}"
        )
        return OpenAICompatibleBackend()
    elif backend_type == "sentence-transformers":
        return SentenceTransformersBackend()
    else:
        raise ValueError(
            f"Unknown embedding backend: '{backend_type}'. "
            f"Supported values: 'sentence-transformers', 'openai-compatible'"
        )
