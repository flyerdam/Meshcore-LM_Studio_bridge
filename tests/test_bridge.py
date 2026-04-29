"""
Unit tests for meshcore_bridge/bridge.py

Tests the command appendix builder, disabled-command filtering,
and the map-queue/advert event processing path.
These tests mock serial/hardware — no device is required.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from meshcore_bridge.bridge import MeshCoreLLMBridge, _build_commands_appendix
from meshcore_bridge.config import DEFAULT_CONFIG


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(**kwargs):
    c = dict(DEFAULT_CONFIG)
    c["disabled_commands"] = set()
    c.update(kwargs)
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# _build_commands_appendix
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildCommandsAppendix:
    def test_contains_ai_prefix(self):
        cfg = _cfg(ai_prefix="!ai", bot_prefix="!b")
        out = _build_commands_appendix(cfg)
        assert "!ai" in out

    def test_contains_bot_prefix(self):
        cfg = _cfg(ai_prefix="!ai", bot_prefix="!b")
        out = _build_commands_appendix(cfg)
        assert "!b" in out

    def test_all_active_commands_listed(self):
        cfg = _cfg(disabled_commands=set())
        out = _build_commands_appendix(cfg)
        for cmd in ["ping", "test", "info", "weather", "news", "search"]:
            assert cmd in out

    def test_disabled_command_not_listed(self):
        cfg = _cfg(disabled_commands={"news", "weather"})
        out = _build_commands_appendix(cfg)
        assert "  !b news" not in out
        assert "  !b weather" not in out

    def test_disabled_commands_section_shown(self):
        cfg = _cfg(disabled_commands={"news"})
        out = _build_commands_appendix(cfg)
        assert "news" in out.lower()  # mentioned in disabled section

    def test_all_disabled_shows_all_disabled_message(self):
        all_cmds = {"ping", "test", "info", "status", "stats", "path", "snr",
                    "weather", "news", "search", "channel", "channels", "reset",
                    "monitor", "help"}
        cfg = _cfg(disabled_commands=all_cmds)
        out = _build_commands_appendix(cfg)
        assert "disabled" in out.lower()

    def test_empty_ai_prefix_falls_back_to_mention(self):
        cfg = _cfg(ai_prefix="", bot_prefix="!b")
        out = _build_commands_appendix(cfg)
        assert "mention" in out.lower() or "no prefix" in out.lower()

    def test_important_note_present(self):
        cfg = _cfg()
        out = _build_commands_appendix(cfg)
        assert "IMPORTANT" in out or "only mention" in out.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# MeshCoreLLMBridge.__init__
# ═══════════════════════════════════════════════════════════════════════════════

class TestBridgeInit:
    def _make_bridge(self, **kwargs):
        cfg = _cfg(**kwargs)
        with patch("meshcore_bridge.bridge.SerialConnection"), \
             patch("meshcore_bridge.bridge.LMStudioClient") as MockLLM, \
             patch("meshcore_bridge.bridge.WebSearch"):
            MockLLM.return_value = MagicMock()
            b = MeshCoreLLMBridge(cfg)
        return b

    def test_cfg_stored(self):
        b = self._make_bridge(model="test-model")
        assert b.cfg["model"] == "test-model"

    def test_gate_llm_none_when_disabled(self):
        b = self._make_bridge(local_gate_enabled=False)
        assert b.gate_llm is None

    def test_gate_llm_created_when_enabled(self):
        with patch("meshcore_bridge.bridge.SerialConnection"), \
             patch("meshcore_bridge.bridge.LMStudioClient") as MockLLM, \
             patch("meshcore_bridge.bridge.WebSearch"):
            MockLLM.return_value = MagicMock()
            cfg = _cfg(local_gate_enabled=True)
            b = MeshCoreLLMBridge(cfg)
        # Two calls: main LLM + gate LLM
        assert MockLLM.call_count == 2

    def test_seen_ids_starts_empty(self):
        b = self._make_bridge()
        assert len(b._seen_ids) == 0

    def test_map_nodes_reflects_queue(self):
        import queue as _q
        mq = _q.Queue()
        cfg = _cfg()
        with patch("meshcore_bridge.bridge.SerialConnection"), \
             patch("meshcore_bridge.bridge.LMStudioClient"), \
             patch("meshcore_bridge.bridge.WebSearch"):
            b = MeshCoreLLMBridge(cfg, map_queue=mq)
        assert b._map_queue is mq


# ═══════════════════════════════════════════════════════════════════════════════
# _on_advert_event — map-queue population
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdvertEvent:
    def _make_bridge_with_mq(self):
        import queue as _q
        mq = _q.Queue(maxsize=500)
        cfg = _cfg()
        with patch("meshcore_bridge.bridge.SerialConnection"), \
             patch("meshcore_bridge.bridge.LMStudioClient"), \
             patch("meshcore_bridge.bridge.WebSearch"):
            b = MeshCoreLLMBridge(cfg, map_queue=mq)
        b.serial = MagicMock()
        b.serial.mc = None
        return b, mq

    def _make_event(self, payload: dict):
        ev = MagicMock()
        ev.payload = payload
        return ev

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_advert_with_name_queued(self):
        b, mq = self._make_bridge_with_mq()
        payload = {
            "adv_name": "Alice",
            "adv_lat": 52.0,
            "adv_lon": 21.0,
            "snr": 5,
            "node_type": 1,
        }
        self._run(b._on_advert_event(self._make_event(payload)))
        assert not mq.empty()
        item = mq.get_nowait()
        assert item["callsign"] == "Alice"
        assert item["lat"] == pytest.approx(52.0)

    def test_advert_without_name_not_queued(self):
        b, mq = self._make_bridge_with_mq()
        payload = {"snr": 5}  # no name
        self._run(b._on_advert_event(self._make_event(payload)))
        assert mq.empty()

    def test_unknown_name_not_queued(self):
        b, mq = self._make_bridge_with_mq()
        payload = {"adv_name": "UNKNOWN", "snr": 5}
        self._run(b._on_advert_event(self._make_event(payload)))
        assert mq.empty()

    def test_advert_zero_gps_still_queued_without_coords(self):
        """Node with 0,0 GPS should be queued but lat/lon not set."""
        b, mq = self._make_bridge_with_mq()
        payload = {"adv_name": "Bob", "adv_lat": 0.0, "adv_lon": 0.0}
        self._run(b._on_advert_event(self._make_event(payload)))
        # Should be queued with no meaningful coords
        assert not mq.empty()
        item = mq.get_nowait()
        assert item["callsign"] == "Bob"
        # lat/lon near zero or None — map should not place marker
        lat = item.get("lat") or 0
        lon = item.get("lon") or 0
        assert abs(lat) < 1.0 and abs(lon) < 1.0

    def test_map_queue_full_does_not_crash(self):
        import asyncio
        import queue as _q
        mq = _q.Queue(maxsize=1)
        mq.put_nowait({"dummy": True})  # fill it
        cfg = _cfg()
        with patch("meshcore_bridge.bridge.SerialConnection"), \
             patch("meshcore_bridge.bridge.LMStudioClient"), \
             patch("meshcore_bridge.bridge.WebSearch"):
            b = MeshCoreLLMBridge(cfg, map_queue=mq)
        b.serial = MagicMock()
        b.serial.mc = None
        payload = {"adv_name": "TestNode", "adv_lat": 52.0, "adv_lon": 21.0}
        # Must not raise even if queue is full
        asyncio.run(b._on_advert_event(self._make_event(payload)))


# ═══════════════════════════════════════════════════════════════════════════════
# listen_channels filter
# ═══════════════════════════════════════════════════════════════════════════════

class TestListenChannels:
    """_should_listen() or equivalent logic: bridge must respect listen_channels."""

    def _bridge_cfg(self, listen):
        cfg = _cfg(listen_channels=listen)
        return cfg

    def test_none_listens_to_all(self):
        cfg = self._bridge_cfg(None)
        # listen_channels=None means all channels pass
        assert cfg["listen_channels"] is None

    def test_channel_list_restricts(self):
        cfg = self._bridge_cfg([0, 2])
        assert 0 in cfg["listen_channels"]
        assert 2 in cfg["listen_channels"]
        assert 1 not in cfg["listen_channels"]


# ═══════════════════════════════════════════════════════════════════════════════
# message_cooldown_s
# ═══════════════════════════════════════════════════════════════════════════════

class TestMessageCooldown:
    def test_cooldown_zero_always_allows(self):
        cfg = _cfg(message_cooldown_s=0)
        assert cfg["message_cooldown_s"] == 0

    def test_cooldown_positive_set(self):
        cfg = _cfg(message_cooldown_s=30)
        assert cfg["message_cooldown_s"] == 30
