"""Unit tests for config.py — configuration loading, parsing, and diffing."""

import pytest

from kiro_ception.config import (
    CLISourceConfig,
    Config,
    EmbeddingConfig,
    IDESourceConfig,
    IndexingConfig,
    MemoryConfig,
    SearchConfig,
    ServerConfig,
    diff_configs,
    expand_path,
)


# --- Config.engine_log_path ---


class TestEngineLogPath:
    """The engine log must live inside the instance's own cache_dir.

    Every other instance-local artifact (embedding DB, engine.lock,
    engine.json) already derives from cache_dir. The log did not, so two
    instances with distinct cache_dirs still shared a single log file.
    """

    def test_auto_is_the_default(self):
        assert Config().server.engine_log_file == "auto"

    def test_auto_follows_cache_dir(self):
        config = Config(embedding=EmbeddingConfig(cache_dir="/tmp/instance-a"))
        assert config.engine_log_path.name == "engine.log"
        assert config.engine_log_path.parent == expand_path("/tmp/instance-a")

    def test_distinct_cache_dirs_yield_distinct_logs(self):
        a = Config(embedding=EmbeddingConfig(cache_dir="/tmp/instance-a"))
        b = Config(embedding=EmbeddingConfig(cache_dir="/tmp/instance-b"))
        assert a.engine_log_path != b.engine_log_path

    def test_empty_string_disables_logging(self):
        config = Config(server=ServerConfig(engine_log_file=""))
        assert config.engine_log_path is None

    def test_whitespace_only_disables_logging(self):
        config = Config(server=ServerConfig(engine_log_file="   "))
        assert config.engine_log_path is None

    def test_explicit_path_is_honored(self):
        config = Config(server=ServerConfig(engine_log_file="~/custom/e.log"))
        assert config.engine_log_path == expand_path("~/custom/e.log")

    def test_isolated_instance_resolves_from_toml(self):
        config = Config.from_dict(
            {
                "embedding": {"cache_dir": "~/.cache/claude-rearview"},
                "server": {"engine_port": 19761, "engine_log_file": "auto"},
            }
        )
        assert config.server.engine_port == 19761
        assert config.engine_log_path == expand_path(
            "~/.cache/claude-rearview/engine.log"
        )


# --- expand_path ---


class TestExpandPath:
    def test_tilde_expansion(self):
        result = expand_path("~/test")
        assert "~" not in str(result)
        assert result.name == "test"

    def test_absolute_path_unchanged(self):
        from pathlib import PurePosixPath, PureWindowsPath
        result = expand_path("/tmp/test")
        # On Windows, Path("/tmp/test") becomes \tmp\test
        assert result.parts[-2:] == ("tmp", "test")

    def test_relative_path(self):
        result = expand_path("relative/path")
        assert result.parts[-2:] == ("relative", "path")


# --- Config.from_dict ---


class TestConfigFromDict:
    def test_empty_dict_uses_defaults(self):
        config = Config.from_dict({})
        assert config.embedding.backend == "sentence-transformers"
        assert config.search.default_threshold == 0.2
        assert config.search.default_max_results == 10
        assert config.indexing.throttle_ms == 0
        assert config.server.engine_port == 19742

    def test_full_config(self):
        data = {
            "sources": {
                "cli": {"enabled": False},
                "ide": {"enabled": True, "patterns": ["/custom/path/*.chat"]},
            },
            "embedding": {
                "backend": "openai-compatible",
                "model": "qwen3-embedding:4b",
                "api_base": "http://localhost:11434/v1",
                "dimensions": 1024,
                "batch_size": 1,
            },
            "search": {
                "default_threshold": 0.3,
                "default_max_results": 20,
                "default_context_window": 5,
            },
            "memory": {"fraction": 0.5, "limit_mb": 2048},
            "indexing": {"throttle_ms": 100, "rescan_interval_minutes": 5},
            "server": {"engine_port": 9999},
        }
        config = Config.from_dict(data)

        assert config.cli.enabled is False
        assert config.ide.enabled is True
        assert config.ide.patterns == ["/custom/path/*.chat"]
        assert config.embedding.backend == "openai-compatible"
        assert config.embedding.model == "qwen3-embedding:4b"
        assert config.embedding.api_base == "http://localhost:11434/v1"
        assert config.embedding.dimensions == 1024
        assert config.embedding.batch_size == 1
        assert config.search.default_threshold == 0.3
        assert config.search.default_max_results == 20
        assert config.search.default_context_window == 5
        assert config.memory.fraction == 0.5
        assert config.memory.limit_mb == 2048
        assert config.indexing.throttle_ms == 100
        assert config.indexing.rescan_interval_minutes == 5
        assert config.server.engine_port == 9999

    def test_partial_config_merges_with_defaults(self):
        data = {
            "embedding": {"model": "custom-model"},
        }
        config = Config.from_dict(data)
        # Specified value overrides default
        assert config.embedding.model == "custom-model"
        # Unspecified values use defaults
        assert config.embedding.backend == "sentence-transformers"
        assert config.embedding.batch_size == 16
        # Other sections use full defaults
        assert config.search.default_threshold == 0.2
        assert config.cli.enabled is True


# --- diff_configs ---


class TestDiffConfigs:
    def test_no_changes(self):
        config = Config()
        changes = diff_configs(config, config)
        assert changes == []

    def test_safe_change_detected(self):
        old = Config()
        new = Config(indexing=IndexingConfig(throttle_ms=500))
        changes = diff_configs(old, new)

        assert len(changes) == 1
        assert changes[0]["key"] == "indexing.throttle_ms"
        assert changes[0]["old"] == 0
        assert changes[0]["new"] == 500
        assert changes[0]["impact"] == "safe"

    def test_breaking_change_detected(self):
        old = Config()
        new = Config(embedding=EmbeddingConfig(model="new-model"))
        changes = diff_configs(old, new)

        model_change = next(c for c in changes if c["key"] == "embedding.model")
        assert model_change["impact"] == "requires_reindex"

    def test_multiple_changes(self):
        old = Config()
        new = Config(
            embedding=EmbeddingConfig(model="new-model", batch_size=32),
            search=SearchConfig(default_threshold=0.5),
        )
        changes = diff_configs(old, new)

        keys = {c["key"] for c in changes}
        assert "embedding.model" in keys
        assert "embedding.batch_size" in keys
        assert "search.default_threshold" in keys

    def test_backend_change_requires_reindex(self):
        old = Config()
        new = Config(embedding=EmbeddingConfig(backend="openai-compatible"))
        changes = diff_configs(old, new)

        assert any(
            c["key"] == "embedding.backend" and c["impact"] == "requires_reindex"
            for c in changes
        )

    def test_dimensions_change_requires_reindex(self):
        old = Config(embedding=EmbeddingConfig(dimensions=384))
        new = Config(embedding=EmbeddingConfig(dimensions=1024))
        changes = diff_configs(old, new)

        assert any(
            c["key"] == "embedding.dimensions" and c["impact"] == "requires_reindex"
            for c in changes
        )

    def test_search_defaults_are_safe(self):
        old = Config()
        new = Config(search=SearchConfig(
            default_threshold=0.5,
            default_max_results=50,
            default_context_window=10,
        ))
        changes = diff_configs(old, new)

        for c in changes:
            assert c["impact"] == "safe"
