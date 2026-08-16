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


# --- Engine startup timeout ---


class TestEngineStartupTimeout:
    """Cold starts (torch + embedding model) can exceed the default wait.

    Overrunning is not fatal — the engine keeps starting and later calls reach
    it — so the default stays short to avoid blocking MCP startup, but it has
    to be raisable on slow machines.
    """

    def test_default_is_thirty_seconds(self):
        assert Config().server.engine_startup_timeout_seconds == 30

    def test_configurable_from_toml(self):
        config = Config.from_dict({"server": {"engine_startup_timeout_seconds": 90}})
        assert config.server.engine_startup_timeout_seconds == 90

    def test_change_is_hot_reloadable(self):
        old = Config()
        new = Config(server=ServerConfig(engine_startup_timeout_seconds=90))
        changes = diff_configs(old, new)
        change = next(
            c for c in changes if c["key"] == "server.engine_startup_timeout_seconds"
        )
        assert change["impact"] == "safe"

    def test_client_reads_the_configured_value(self, monkeypatch):
        from kiro_ception import engine_client

        config = Config(server=ServerConfig(engine_startup_timeout_seconds=90))
        monkeypatch.setattr(engine_client, "get_config", lambda: config)
        assert engine_client._startup_timeout() == 90

    def test_client_falls_back_when_value_is_nonsense(self, monkeypatch):
        from kiro_ception import engine_client

        for bad in (0, -5):
            config = Config(server=ServerConfig(engine_startup_timeout_seconds=bad))
            monkeypatch.setattr(engine_client, "get_config", lambda c=config: c)
            assert engine_client._startup_timeout() == 30

    def test_client_falls_back_when_config_unreadable(self, monkeypatch):
        from kiro_ception import engine_client

        def boom():
            raise RuntimeError("no config")

        monkeypatch.setattr(engine_client, "get_config", boom)
        assert engine_client._startup_timeout() == 30


# --- Instance identity ---


class TestInstanceIdentity:
    """Concurrent instances expose identical tool names, so every tool
    description has to state which sources its instance indexes."""

    def test_all_sources_listed_most_distinguishing_first(self):
        assert Config().indexed_sources == ["Claude Code", "Kiro IDE", "Kiro CLI"]

    def test_disabled_sources_omitted(self):
        from kiro_ception.config import ClaudeSourceConfig

        config = Config(
            claude=ClaudeSourceConfig(enabled=False),
            cli=CLISourceConfig(enabled=False),
        )
        assert config.indexed_sources == ["Kiro IDE"]

    def test_summary_without_label(self):
        assert Config().instance_summary == (
            "Indexes: Claude Code, Kiro IDE, Kiro CLI."
        )

    def test_summary_with_label(self):
        config = Config(server=ServerConfig(instance_label="claude-rearview"))
        assert config.instance_summary == (
            'Instance "claude-rearview". Indexes: Claude Code, Kiro IDE, Kiro CLI.'
        )

    def test_label_whitespace_ignored(self):
        config = Config(server=ServerConfig(instance_label="   "))
        assert config.instance_summary.startswith("Indexes:")

    def test_summary_when_everything_disabled(self):
        from kiro_ception.config import ClaudeSourceConfig

        config = Config(
            claude=ClaudeSourceConfig(enabled=False),
            ide=IDESourceConfig(enabled=False),
            cli=CLISourceConfig(enabled=False),
        )
        assert config.indexed_sources == []
        assert "nothing (all sources are disabled)" in config.instance_summary

    def test_two_instances_are_distinguishable(self):
        from kiro_ception.config import ClaudeSourceConfig

        claude_instance = Config(
            server=ServerConfig(instance_label="claude-rearview")
        )
        kiro_instance = Config(
            server=ServerConfig(instance_label="kiro-ception"),
            claude=ClaudeSourceConfig(enabled=False),
        )
        assert claude_instance.instance_summary != kiro_instance.instance_summary
        assert "Claude Code" in claude_instance.instance_summary
        assert "Claude Code" not in kiro_instance.instance_summary

    def test_label_defaults_to_empty(self):
        assert Config().server.instance_label == ""

    def test_label_parsed_from_toml(self):
        config = Config.from_dict({"server": {"instance_label": "alt"}})
        assert config.server.instance_label == "alt"
        assert config.instance_summary.startswith('Instance "alt".')


class TestAutoDerivedInstanceLabel:
    """"auto" derives a label from resources that are unique per instance.

    cache_dir is unique by construction (the engine lock lives there) and the
    engine port is unique among running instances (only one process can bind
    it), so either is a sound key.
    """

    def _config(self, cache_dir: str, port: int = 19742) -> Config:
        return Config(
            embedding=EmbeddingConfig(cache_dir=cache_dir),
            server=ServerConfig(instance_label="auto", engine_port=port),
        )

    def test_derives_from_cache_dir_name(self):
        config = self._config("~/.cache/claude-rearview")
        assert config.resolved_instance_label == "claude-rearview"

    def test_looks_past_a_generic_container_name(self):
        # <root>/claude-rearview/cache — "cache" identifies nothing.
        config = self._config("/opt/claude-rearview/cache")
        assert config.resolved_instance_label == "claude-rearview"

    def test_leading_dot_stripped(self):
        config = self._config("/opt/.claude-rearview")
        assert config.resolved_instance_label == "claude-rearview"

    def test_falls_back_to_port_when_all_names_are_generic(self):
        config = self._config("/var/cache", port=19761)
        assert config.resolved_instance_label == "port-19761"

    def test_two_instances_derive_different_labels(self):
        a = self._config("~/.cache/kiro-ception", port=19742)
        b = self._config("~/.cache/claude-rearview", port=19761)
        assert a.resolved_instance_label != b.resolved_instance_label

    def test_derived_label_reaches_the_summary(self):
        config = self._config("~/.cache/claude-rearview")
        assert config.instance_summary.startswith('Instance "claude-rearview".')

    def test_explicit_label_wins_over_auto(self):
        config = Config(
            embedding=EmbeddingConfig(cache_dir="~/.cache/claude-rearview"),
            server=ServerConfig(instance_label="my-name"),
        )
        assert config.resolved_instance_label == "my-name"

    def test_empty_stays_unlabelled(self):
        config = Config(embedding=EmbeddingConfig(cache_dir="~/.cache/whatever"))
        assert config.resolved_instance_label == ""
        assert config.instance_summary.startswith("Indexes:")

    def test_derivation_is_stable_across_calls(self):
        config = self._config("~/.cache/claude-rearview")
        assert config.resolved_instance_label == config.resolved_instance_label


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
