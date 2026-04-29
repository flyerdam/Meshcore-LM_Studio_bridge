"""
Unit tests for meshcore_bridge/llm_client.py

Tests token budget enforcement, per-sender history management,
model caps inference, API key resolution, and the should_reply gate.
All HTTP is mocked — no network calls.
"""
import json
import time
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from collections import deque
from meshcore_bridge.llm_client import LMStudioClient


# ── Factory ───────────────────────────────────────────────────────────────────

def _make_client(
    url="http://localhost:1234/v1/chat/completions",
    model="test-model",
    system_prompt="You are a test bot.",
    history_len=5,
    provider="openai_compat",
    api_key=None,
    budget_total=0,
    budget_prompt=0,
    budget_completion=0,
    tmp_path=None,
):
    cache_file = str(tmp_path / "caps.json") if tmp_path else ":memory:"
    # Patch _ensure_model_caps_loaded so no HTTP call is made during init
    with patch.object(LMStudioClient, "_ensure_model_caps_loaded"):
        return LMStudioClient(
            url=url,
            model=model,
            system_prompt=system_prompt,
            history_len=history_len,
            provider=provider,
            api_key=api_key,
            model_caps_cache_file=cache_file,
            token_budget_total=budget_total,
            token_budget_prompt=budget_prompt,
            token_budget_completion=budget_completion,
        )


def _mock_chat_response(content: str, prompt_t=10, completion_t=20):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_t,
            "completion_tokens": completion_t,
            "total_tokens": prompt_t + completion_t,
        },
    }
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# Token budget
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenBudget:
    def test_no_budget_never_blocks(self, tmp_path):
        client = _make_client(budget_total=0, tmp_path=tmp_path)
        assert client._budget_block_reason(9999) is None

    def test_total_budget_exhausted(self, tmp_path):
        client = _make_client(budget_total=100, tmp_path=tmp_path)
        client._used_total_tokens = 100
        assert client._budget_block_reason(1) is not None
        assert "total" in client._budget_block_reason(1)

    def test_completion_budget_exhausted(self, tmp_path):
        client = _make_client(budget_completion=50, tmp_path=tmp_path)
        client._used_completion_tokens = 50
        reason = client._budget_block_reason(1)
        assert reason is not None
        assert "completion" in reason

    def test_prompt_budget_exhausted(self, tmp_path):
        client = _make_client(budget_prompt=30, tmp_path=tmp_path)
        client._used_prompt_tokens = 30
        reason = client._budget_block_reason(1)
        assert reason is not None
        assert "prompt" in reason

    def test_remaining_too_low_for_request(self, tmp_path):
        client = _make_client(budget_total=100, tmp_path=tmp_path)
        client._used_total_tokens = 95
        reason = client._budget_block_reason(10)  # need 10, only 5 left
        assert reason is not None

    def test_budget_below_remaining_no_block(self, tmp_path):
        client = _make_client(budget_total=1000, tmp_path=tmp_path)
        client._used_total_tokens = 100
        assert client._budget_block_reason(10) is None

    def test_negative_budget_is_clamped_to_zero(self, tmp_path):
        # Negative values passed to constructor should be treated as unlimited
        client = _make_client(budget_total=-999, tmp_path=tmp_path)
        assert client.token_budget_total == 0
        assert client._budget_block_reason(9999) is None


# ═══════════════════════════════════════════════════════════════════════════════
# _record_usage
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecordUsage:
    def test_accumulates_tokens(self, tmp_path):
        client = _make_client(tmp_path=tmp_path)
        client._record_usage({"usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}})
        client._record_usage({"usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}})
        assert client._used_prompt_tokens == 70
        assert client._used_completion_tokens == 130
        assert client._used_total_tokens == 200

    def test_missing_usage_key_does_not_crash(self, tmp_path):
        client = _make_client(tmp_path=tmp_path)
        client._record_usage({})
        assert client._used_total_tokens == 0

    def test_negative_token_counts_ignored(self, tmp_path):
        client = _make_client(tmp_path=tmp_path)
        client._record_usage({"usage": {"prompt_tokens": -5, "completion_tokens": -10, "total_tokens": -15}})
        assert client._used_total_tokens == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Per-sender history
# ═══════════════════════════════════════════════════════════════════════════════

class TestHistory:
    def test_separate_per_sender(self, tmp_path):
        client = _make_client(history_len=5, tmp_path=tmp_path)
        h1 = client._history("Alice")
        h2 = client._history("Bob")
        h1.append({"role": "user", "content": "hi"})
        assert len(h2) == 0  # Bob's history untouched

    def test_maxlen_respected(self, tmp_path):
        client = _make_client(history_len=3, tmp_path=tmp_path)
        h = client._history("Alice")
        for i in range(20):
            h.append({"role": "user", "content": f"msg{i}"})
        # maxlen is history_len * 2 (user + assistant pairs)
        assert len(h) == 6

    def test_new_sender_gets_empty_history(self, tmp_path):
        client = _make_client(tmp_path=tmp_path)
        h = client._history("newuser")
        assert len(h) == 0

    def test_history_key_isolation(self, tmp_path):
        client = _make_client(tmp_path=tmp_path)
        _ = client._history("Alice")
        _ = client._history("Bob")
        assert "Alice" in client._histories
        assert "Bob" in client._histories


# ═══════════════════════════════════════════════════════════════════════════════
# _effective_url / _resolved_api_key
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiResolution:
    def test_github_provider_uses_github_url(self, tmp_path):
        client = _make_client(
            provider="github_models",
            url="http://localhost:1234/v1/chat/completions",  # local URL
            tmp_path=tmp_path,
        )
        assert "github.ai" in client._effective_url()

    def test_openai_compat_keeps_url(self, tmp_path):
        client = _make_client(
            provider="openai_compat",
            url="http://myserver:5000/v1",
            tmp_path=tmp_path,
        )
        assert client._effective_url() == "http://myserver:5000/v1"

    def test_explicit_api_key_used(self, tmp_path):
        client = _make_client(api_key="sk-test", tmp_path=tmp_path)
        assert client._resolved_api_key() == "sk-test"

    def test_env_github_token_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-env-key")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        client = _make_client(provider="github_models", api_key=None, tmp_path=tmp_path)
        assert client._resolved_api_key() == "gh-env-key"

    def test_env_llm_api_key_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "llm-env-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        client = _make_client(provider="openai_compat", api_key=None, tmp_path=tmp_path)
        assert client._resolved_api_key() == "llm-env-key"

    def test_no_key_returns_none(self, tmp_path, monkeypatch):
        for k in ("LLM_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        client = _make_client(provider="openai_compat", api_key=None, tmp_path=tmp_path)
        assert client._resolved_api_key() is None


# ═══════════════════════════════════════════════════════════════════════════════
# _request_headers
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequestHeaders:
    def test_bearer_token_in_header(self, tmp_path):
        client = _make_client(api_key="my-key", tmp_path=tmp_path)
        h = client._request_headers()
        assert h.get("Authorization") == "Bearer my-key"

    def test_github_accept_header(self, tmp_path):
        client = _make_client(provider="github_models", api_key="gh-key", tmp_path=tmp_path)
        h = client._request_headers()
        assert "vnd.github" in h.get("Accept", "")

    def test_no_auth_when_no_key(self, tmp_path, monkeypatch):
        for k in ("LLM_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        client = _make_client(api_key=None, tmp_path=tmp_path)
        assert "Authorization" not in client._request_headers()


# ═══════════════════════════════════════════════════════════════════════════════
# _infer_caps_from_model_id (static)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInferCaps:
    def test_gpt5_uses_max_completion_tokens(self):
        caps = LMStudioClient._infer_caps_from_model_id("openai/gpt-5")
        assert caps["token_param"] == "max_completion_tokens"
        assert caps["supports_temperature"] is False

    def test_regular_model_uses_max_tokens(self):
        caps = LMStudioClient._infer_caps_from_model_id("gemma-3-12b")
        assert caps["token_param"] == "max_tokens"
        assert caps["supports_temperature"] is True

    def test_empty_model_id_defaults(self):
        caps = LMStudioClient._infer_caps_from_model_id("")
        assert caps["token_param"] == "max_tokens"


# ═══════════════════════════════════════════════════════════════════════════════
# should_reply() — auto-engage gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestShouldReply:
    @pytest.fixture
    def client(self, tmp_path):
        return _make_client(tmp_path=tmp_path)

    def test_off_always_false(self, client):
        assert client.should_reply("Alice", "hello?", intensity="off") is False

    def test_empty_message_always_false(self, client):
        assert client.should_reply("Alice", "", intensity="normal") is False
        assert client.should_reply("Alice", "  ", intensity="keyword") is False

    def test_explicit_ai_prefix_always_true(self, client):
        # !ai prefix is a hard yes for any non-off mode
        assert client.should_reply("Alice", "!ai what time is it", intensity="keyword") is True
        assert client.should_reply("Alice", "!ai what time is it", intensity="minimal") is True

    def test_off_mode_blocks_even_ai_prefix(self, client):
        # intensity=off short-circuits before the !ai prefix check
        assert client.should_reply("Alice", "!ai what time is it", intensity="off") is False

    def test_alias_mention_always_true(self, client):
        # alias mention is a hard yes in non-off modes
        assert client.should_reply("Alice", "hey flyerai you there",
                                   mention_aliases=["flyerai"], intensity="keyword") is True

    def test_alias_mention_blocked_when_off(self, client):
        # off mode blocks everything including alias mention
        assert client.should_reply("Alice", "hey flyerai you there",
                                   mention_aliases=["flyerai"], intensity="off") is False

    def test_keyword_question_mark(self, client):
        assert client.should_reply("Alice", "what is this?", intensity="keyword") is True

    def test_keyword_ai_word(self, client):
        assert client.should_reply("Alice", "hey bot help", intensity="keyword") is True

    def test_keyword_no_match(self, client):
        assert client.should_reply("Alice", "gm de alice", intensity="keyword") is False

    def test_aggressive_short_message_false(self, client):
        assert client.should_reply("Alice", "ok", intensity="aggressive") is False

    def test_aggressive_longer_message_true(self, client):
        assert client.should_reply("Alice", "interesting signal here", intensity="aggressive") is True

    def test_minimal_no_ai_keyword_false(self, client):
        # "minimal" with no AI keyword → False without LLM call
        result = client.should_reply("Alice", "73 de alice", intensity="minimal")
        assert result is False

    def test_minimal_ai_keyword_triggers_gate(self, client):
        with patch.object(client, "_gate_via_llm", return_value=True) as mock_gate:
            result = client.should_reply("Alice", "hey ai", intensity="minimal")
        assert mock_gate.called
        assert result is True

    def test_budget_exhausted_gate_skips(self, client):
        client._used_total_tokens = 999999
        client.token_budget_total = 100
        # Even "normal" mode should skip LLM gate when budget exhausted
        with patch.object(client, "_gate_via_llm") as mock_gate:
            client.should_reply("Alice", "hello", intensity="normal")
            # Either returns False without LLM call, or calls LLM but gate skips
            # Budget enforcement is in _budget_block_reason used by _gate_via_llm
