"""Integration tests for embeddings.py — backend abstraction and factory.

Tests the OpenAI-compatible backend with a mocked HTTP layer,
and the factory function's configuration routing.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from kiro_ception.config import Config, EmbeddingConfig
from kiro_ception.embeddings import (
    EmbeddingBackend,
    OpenAICompatibleBackend,
    get_embedding_backend,
)


# --- get_embedding_backend factory ---


class TestGetEmbeddingBackend:
    def test_sentence_transformers_backend(self):
        config = Config(embedding=EmbeddingConfig(backend="sentence-transformers"))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            backend = get_embedding_backend()
            assert backend.fingerprint() == "sentence-transformers:all-MiniLM-L6-v2"

    def test_openai_compatible_backend(self):
        config = Config(embedding=EmbeddingConfig(
            backend="openai-compatible",
            api_base="http://localhost:11434/v1",
            model="qwen3-embedding:4b",
            dimensions=1024,
        ))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            backend = get_embedding_backend()
            assert isinstance(backend, OpenAICompatibleBackend)
            assert "openai-compatible" in backend.fingerprint()
            assert "qwen3-embedding:4b" in backend.fingerprint()

    def test_openai_compatible_requires_api_base(self):
        config = Config(embedding=EmbeddingConfig(
            backend="openai-compatible",
            api_base="",
        ))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            with pytest.raises(ValueError, match="api_base is required"):
                get_embedding_backend()

    def test_unknown_backend_raises(self):
        config = Config(embedding=EmbeddingConfig(backend="invalid"))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            with pytest.raises(ValueError, match="Unknown embedding backend"):
                get_embedding_backend()


# --- OpenAICompatibleBackend ---


class TestOpenAICompatibleBackend:
    @pytest.fixture
    def backend(self):
        config = Config(embedding=EmbeddingConfig(
            backend="openai-compatible",
            api_base="http://localhost:11434/v1",
            model="qwen3-embedding:4b",
            dimensions=1024,
            batch_size=2,
        ))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            return OpenAICompatibleBackend()

    def _mock_response(self, embeddings: list[list[float]]):
        """Create a mock response matching OpenAI embeddings format."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "data": [{"embedding": emb} for emb in embeddings],
        }
        return response

    def test_encode_query_returns_normalized_vector(self, backend):
        # Return a non-normalized vector to verify normalization
        raw_emb = [1.0, 2.0, 3.0] + [0.0] * 1021
        mock_response = self._mock_response([raw_emb])

        with patch.object(backend, "_get_session") as mock_session:
            mock_session.return_value.post.return_value = mock_response

            result = backend.encode_query("test query")

            assert result.shape == (1024,)
            # Check L2 normalization
            norm = np.linalg.norm(result)
            assert abs(norm - 1.0) < 1e-5

    def test_encode_batches_requests(self, backend):
        """With batch_size=2 and 5 texts, should make 3 API calls."""
        raw_emb = [[0.1] * 1024]

        mock_response = self._mock_response(raw_emb * 2)
        mock_response_last = self._mock_response(raw_emb)

        with (
            patch.object(backend, "_get_session") as mock_session,
            patch("kiro_ception.embeddings.get_config") as mock_config,
        ):
            config = Config(embedding=EmbeddingConfig(
                backend="openai-compatible",
                api_base="http://localhost:11434/v1",
                model="qwen3-embedding:4b",
                dimensions=1024,
                batch_size=2,
            ))
            mock_config.return_value = config
            mock_session.return_value.post.side_effect = [
                mock_response, mock_response, mock_response_last
            ]

            result = backend.encode(["text1", "text2", "text3", "text4", "text5"])

            assert result.shape == (5, 1024)
            assert mock_session.return_value.post.call_count == 3

    def test_encode_empty_list(self, backend):
        with patch("kiro_ception.embeddings.get_config") as mock_config:
            config = Config(embedding=EmbeddingConfig(
                backend="openai-compatible",
                api_base="http://localhost:11434/v1",
                model="qwen3-embedding:4b",
                dimensions=1024,
                batch_size=2,
            ))
            mock_config.return_value = config

            result = backend.encode([])
            assert result.size == 0

    def test_fingerprint_includes_dimensions(self, backend):
        fp = backend.fingerprint()
        assert "1024" in fp
        assert "qwen3-embedding:4b" in fp
        assert "localhost:11434" in fp

    def test_fingerprint_without_dimensions(self):
        config = Config(embedding=EmbeddingConfig(
            backend="openai-compatible",
            api_base="http://localhost:11434/v1",
            model="nomic-embed",
            dimensions=None,
        ))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            backend = OpenAICompatibleBackend()
            fp = backend.fingerprint()
            assert "nomic-embed" in fp
            # No trailing :None or :0
            assert fp.endswith("nomic-embed")

    def test_dimensions_from_config(self, backend):
        assert backend.dimensions() == 1024

    def test_api_error_propagates(self, backend):
        """HTTP errors should propagate up."""
        import requests

        with patch.object(backend, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
            mock_session.return_value.post.return_value = mock_response

            with pytest.raises(requests.HTTPError):
                backend.encode_query("test")


# --- SentenceTransformersBackend model loading ---


class TestSentenceTransformersModelLoad:
    """The model is loaded from the local HF cache when possible.

    SentenceTransformer otherwise revalidates against the Hub on every load —
    measured at ~6s per engine start for an already-cached model, plus a hard
    dependency on network reachability.
    """

    def _backend(self, model="all-MiniLM-L6-v2"):
        from kiro_ception.config import Config, EmbeddingConfig
        from kiro_ception.embeddings import SentenceTransformersBackend

        config = Config(embedding=EmbeddingConfig(model=model))
        with patch("kiro_ception.embeddings.get_config", return_value=config):
            return SentenceTransformersBackend()

    def test_loads_from_local_cache_first(self):
        backend = self._backend()
        fake = MagicMock()
        with patch.dict(
            "sys.modules",
            {"sentence_transformers": MagicMock(SentenceTransformer=fake)},
        ):
            backend._get_model()

        fake.assert_called_once_with("all-MiniLM-L6-v2", local_files_only=True)

    def test_falls_back_to_download_when_not_cached(self):
        backend = self._backend()
        sentinel = object()

        def side_effect(name, **kwargs):
            if kwargs.get("local_files_only"):
                raise OSError("not in cache")
            return sentinel

        fake = MagicMock(side_effect=side_effect)
        with patch.dict(
            "sys.modules",
            {"sentence_transformers": MagicMock(SentenceTransformer=fake)},
        ):
            model = backend._get_model()

        assert model is sentinel
        assert fake.call_count == 2
        # Second attempt must not pin to the local cache.
        assert fake.call_args_list[1] == (("all-MiniLM-L6-v2",), {})

    def test_model_is_cached_on_the_backend(self):
        backend = self._backend()
        fake = MagicMock()
        with patch.dict(
            "sys.modules",
            {"sentence_transformers": MagicMock(SentenceTransformer=fake)},
        ):
            first = backend._get_model()
            second = backend._get_model()

        assert first is second
        fake.assert_called_once()
