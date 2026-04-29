"""
Shared fixtures and helpers for the MeshCore-LM Studio Bridge test suite.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from meshcore_bridge.config import DEFAULT_CONFIG


# ── Config fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def cfg():
    """Minimal runtime config derived from DEFAULT_CONFIG."""
    c = dict(DEFAULT_CONFIG)
    c["disabled_commands"] = set()
    return c


@pytest.fixture
def cfg_no_prefix(cfg):
    """Config with empty bot/ai prefix (bare-word commands)."""
    cfg["bot_prefix"] = ""
    cfg["ai_prefix"] = ""
    return cfg


# ── Stub LLM client ───────────────────────────────────────────────────────────

@pytest.fixture
def stub_llm():
    llm = MagicMock()
    llm.analyze.return_value = "stub analysis"
    llm.query.return_value = "stub reply"
    return llm


# ── Stub web search ───────────────────────────────────────────────────────────

@pytest.fixture
def stub_web():
    web = MagicMock()
    web.weather.return_value = "London(GB): clear sky 18°C wind:3m/s"
    web.news.return_value = "Headline 1 | Headline 2 | Headline 3"
    web.search.return_value = "DuckDuckGo result"
    return web


# ── Stub MeshCore mc object ───────────────────────────────────────────────────

@pytest.fixture
def stub_mc():
    mc = MagicMock()
    mc.commands = MagicMock()
    return mc


# ── Minimal BotCommands factory ───────────────────────────────────────────────

@pytest.fixture
def make_bot(cfg, stub_llm, stub_web, stub_mc):
    from meshcore_bridge.bot_commands import BotCommands

    def _make(device_info=None, telemetry=None, extra_cfg=None):
        c = dict(cfg)
        if extra_cfg:
            c.update(extra_cfg)
        return BotCommands(
            device_info=device_info or {},
            cfg=c,
            llm=stub_llm,
            web=stub_web,
            telemetry=telemetry or {},
            mc=stub_mc,
        )
    return _make


# ── Async test helper ─────────────────────────────────────────────────────────

def run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Sample payloads ───────────────────────────────────────────────────────────

PAYLOAD_GOOD = {
    "snr": 7,
    "rssi": -90,
    "path_len": 1,
    "sender_timestamp": 1_700_000_000,
}

PAYLOAD_WEAK = {
    "snr": -15,
    "rssi": -120,
    "path_len": 5,
    "sender_timestamp": 0,
}

PAYLOAD_EMPTY = {}
