"""
Unit tests for meshcore_bridge/config.py

Tests DEFAULT_CONFIG structure, normalization, JSON sanitization,
and legacy config migration.
"""
import json
import pytest
from pathlib import Path
from meshcore_bridge.config import (
    BYTE_LIMIT,
    DEFAULT_CONFIG,
    _json_sanitize,
    _normalize_loaded,
    load_persisted_config,
    save_user_config,
)


# ═══════════════════════════════════════════════════════════════════════════════
# BYTE_LIMIT
# ═══════════════════════════════════════════════════════════════════════════════

class TestByteLimit:
    def test_value_reasonable(self):
        # 141 byte mesh MTU with UTF-8 margin
        assert 100 <= BYTE_LIMIT <= 141

    def test_is_int(self):
        assert isinstance(BYTE_LIMIT, int)


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT_CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

class TestDefaultConfig:
    REQUIRED_KEYS = [
        "serial_port", "baud_rate", "llm_provider", "lm_url", "model",
        "ai_prefix", "bot_prefix", "system_prompt", "history_len",
        "max_chunks", "reply_delay_s", "listen_channels",
        "poll_interval_s", "channel_history_len", "message_cooldown_s",
        "disabled_commands", "ai_enabled", "auto_engage_intensity",
    ]

    def test_all_required_keys_present(self):
        for key in self.REQUIRED_KEYS:
            assert key in DEFAULT_CONFIG, f"Missing key: {key}"

    def test_disabled_commands_is_set(self):
        assert isinstance(DEFAULT_CONFIG["disabled_commands"], set)

    def test_baud_rate_is_int(self):
        assert isinstance(DEFAULT_CONFIG["baud_rate"], int)

    def test_history_len_positive(self):
        assert DEFAULT_CONFIG["history_len"] > 0

    def test_max_chunks_positive(self):
        assert DEFAULT_CONFIG["max_chunks"] > 0

    def test_system_prompt_not_empty(self):
        assert len(DEFAULT_CONFIG["system_prompt"]) > 50

    def test_auto_engage_valid(self):
        valid = {"off", "minimal", "keyword", "normal", "aggressive"}
        assert DEFAULT_CONFIG["auto_engage_intensity"] in valid


# ═══════════════════════════════════════════════════════════════════════════════
# _json_sanitize
# ═══════════════════════════════════════════════════════════════════════════════

class TestJsonSanitize:
    def test_set_becomes_sorted_list(self):
        cfg = {"disabled_commands": {"news", "ping", "weather"}}
        result = _json_sanitize(cfg)
        assert isinstance(result["disabled_commands"], list)
        assert result["disabled_commands"] == sorted(cfg["disabled_commands"])

    def test_empty_set_becomes_empty_list(self):
        cfg = {"disabled_commands": set()}
        result = _json_sanitize(cfg)
        assert result["disabled_commands"] == []

    def test_non_set_field_unchanged(self):
        cfg = {"baud_rate": 115200, "model": "gemma"}
        result = _json_sanitize(cfg)
        assert result["baud_rate"] == 115200
        assert result["model"] == "gemma"

    def test_original_not_mutated(self):
        original = {"disabled_commands": {"ping"}}
        _json_sanitize(original)
        assert isinstance(original["disabled_commands"], set)


# ═══════════════════════════════════════════════════════════════════════════════
# _normalize_loaded
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeLoaded:
    def test_list_disabled_commands_becomes_set(self):
        cfg = {"disabled_commands": ["ping", "news"]}
        result = _normalize_loaded(cfg)
        assert isinstance(result["disabled_commands"], set)
        assert "ping" in result["disabled_commands"]

    def test_empty_listen_channels_becomes_none(self):
        for val in ("", [], (), {}):
            result = _normalize_loaded({"listen_channels": val})
            assert result["listen_channels"] is None

    def test_string_listen_channels_parsed(self):
        result = _normalize_loaded({"listen_channels": "0 2 5"})
        assert result["listen_channels"] == [0, 2, 5]

    def test_empty_string_api_key_becomes_none(self):
        result = _normalize_loaded({"llm_api_key": ""})
        assert result["llm_api_key"] is None

    def test_empty_string_news_key_becomes_none(self):
        result = _normalize_loaded({"news_api_key": ""})
        assert result["news_api_key"] is None

    def test_reply_channel_string_converted_to_int(self):
        result = _normalize_loaded({"reply_channel": "3"})
        assert result["reply_channel"] == 3

    def test_reply_channel_garbage_becomes_none(self):
        result = _normalize_loaded({"reply_channel": "abc"})
        assert result["reply_channel"] is None

    def test_intensity_migration_cautious(self):
        result = _normalize_loaded({"auto_engage_intensity": "cautious"})
        assert result["auto_engage_intensity"] == "keyword"

    def test_intensity_valid_unchanged(self):
        result = _normalize_loaded({"auto_engage_intensity": "normal"})
        assert result["auto_engage_intensity"] == "normal"

    def test_gate_api_key_none_string_becomes_none(self):
        for val in ("None", "none", "", None):
            result = _normalize_loaded({"local_gate_api_key": val})
            assert result["local_gate_api_key"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# load_persisted_config / save_user_config (file-based round-trip)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistConfig:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        from meshcore_bridge import config as _cfg_mod
        user_file = tmp_path / "bridge_config.user.json"
        monkeypatch.setattr(_cfg_mod, "USER_CONFIG_FILE", user_file)
        monkeypatch.setattr(_cfg_mod, "DEFAULT_CONFIG_FILE", tmp_path / "no.json")
        monkeypatch.setattr(_cfg_mod, "LEGACY_GUI_CONFIG_FILE", tmp_path / "no2.json")

        overrides = {"model": "test-model", "history_len": 7}
        save_user_config(overrides)

        loaded = load_persisted_config()
        assert loaded["model"] == "test-model"
        assert loaded["history_len"] == 7

    def test_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        from meshcore_bridge import config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "USER_CONFIG_FILE", tmp_path / "none.json")
        monkeypatch.setattr(_cfg_mod, "DEFAULT_CONFIG_FILE", tmp_path / "none2.json")
        monkeypatch.setattr(_cfg_mod, "LEGACY_GUI_CONFIG_FILE", tmp_path / "none3.json")

        loaded = load_persisted_config()
        # Must contain the required defaults
        assert "serial_port" in loaded
        assert "bot_prefix" in loaded

    def test_disabled_commands_survives_roundtrip(self, tmp_path, monkeypatch):
        from meshcore_bridge import config as _cfg_mod
        user_file = tmp_path / "bridge_config.user.json"
        monkeypatch.setattr(_cfg_mod, "USER_CONFIG_FILE", user_file)
        monkeypatch.setattr(_cfg_mod, "DEFAULT_CONFIG_FILE", tmp_path / "no.json")
        monkeypatch.setattr(_cfg_mod, "LEGACY_GUI_CONFIG_FILE", tmp_path / "no2.json")

        save_user_config({"disabled_commands": {"news", "ping"}})
        loaded = load_persisted_config()
        assert isinstance(loaded["disabled_commands"], set)
        assert "news" in loaded["disabled_commands"]
        assert "ping" in loaded["disabled_commands"]
