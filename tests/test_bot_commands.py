"""
Unit tests for meshcore_bridge/bot_commands.py

Covers command parsing, dispatch, per-command logic, disabled commands,
response byte limits, and async command paths.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from meshcore_bridge.bot_commands import BotCommands
from meshcore_bridge.config import BYTE_LIMIT
from tests.conftest import PAYLOAD_GOOD, PAYLOAD_WEAK, PAYLOAD_EMPTY


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= BYTE_LIMIT


# ═══════════════════════════════════════════════════════════════════════════════
# match()
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatch:
    def test_known_command(self, make_bot):
        bot = make_bot()
        cmd, rest = bot.match("ping")
        assert cmd == "_cmd_ping"
        assert rest == ""

    def test_command_with_args(self, make_bot):
        bot = make_bot()
        cmd, rest = bot.match("weather London")
        assert cmd == "_cmd_weather"
        assert rest == "London"

    def test_unknown_returns_none(self, make_bot):
        bot = make_bot()
        cmd, rest = bot.match("nonsense")
        assert cmd is None

    def test_empty_string(self, make_bot):
        bot = make_bot()
        cmd, rest = bot.match("")
        assert cmd is None

    def test_case_insensitive(self, make_bot):
        bot = make_bot()
        cmd, _ = bot.match("PING")
        assert cmd == "_cmd_ping"

    def test_status_alias(self, make_bot):
        bot = make_bot()
        cmd, _ = bot.match("status")
        assert cmd == "_cmd_info"  # 'status' maps to _cmd_info


# ═══════════════════════════════════════════════════════════════════════════════
# handle() — disabled commands
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandleDisabled:
    def test_disabled_command_returns_empty(self, make_bot):
        bot = make_bot(extra_cfg={"disabled_commands": {"ping"}})
        result = _run(bot.handle("_cmd_ping", "", "Alice", PAYLOAD_GOOD, 0))
        assert result == ""

    def test_enabled_command_returns_content(self, make_bot):
        bot = make_bot()
        result = _run(bot.handle("_cmd_ping", "", "Alice", PAYLOAD_GOOD, 0))
        assert "Alice" in result
        assert len(result) > 0

    def test_unknown_method_returns_empty(self, make_bot):
        bot = make_bot()
        result = _run(bot.handle("_cmd_nonexistent", "", "Alice", PAYLOAD_GOOD, 0))
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_ping
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdPing:
    def test_response_contains_sender(self, make_bot):
        bot = make_bot()
        r = bot._cmd_ping("", "Waldcor", PAYLOAD_GOOD, 0)
        assert "Waldcor" in r

    def test_response_contains_pong(self, make_bot):
        bot = make_bot()
        r = bot._cmd_ping("", "X", PAYLOAD_GOOD, 0)
        assert "Pong" in r

    def test_response_fits_byte_limit(self, make_bot):
        bot = make_bot()
        r = bot._cmd_ping("", "A" * 20, PAYLOAD_GOOD, 0)
        assert _fits(r)

    def test_weak_signal_still_responds(self, make_bot):
        bot = make_bot()
        r = bot._cmd_ping("", "Y", PAYLOAD_WEAK, 0)
        assert "Pong" in r
        assert "critical" in r or "very weak" in r

    def test_empty_payload_no_crash(self, make_bot):
        bot = make_bot()
        r = bot._cmd_ping("", "Z", PAYLOAD_EMPTY, 0)
        assert "Pong" in r


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_test
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdTest:
    def test_response_contains_ack(self, make_bot):
        bot = make_bot()
        r = bot._cmd_test("", "Alice", PAYLOAD_GOOD, 0)
        assert "Ack" in r or "✅" in r

    def test_response_fits_byte_limit(self, make_bot):
        bot = make_bot()
        r = bot._cmd_test("", "Alice", PAYLOAD_GOOD, 0)
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_info
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdInfo:
    def test_basic_response(self, make_bot):
        device = {"model": "Heltec V3", "ver": "2.0"}
        bot = make_bot(device_info=device)
        r = bot._cmd_info("", "Alice", PAYLOAD_GOOD, 0)
        assert "Heltec V3" in r
        assert "Alice" in r

    def test_battery_from_millivolts(self, make_bot):
        device = {"model": "X", "ver": "1"}
        telemetry = {"batt_milli_volts": 3900}
        bot = make_bot(device_info=device, telemetry=telemetry)
        r = bot._cmd_info("", "Alice", PAYLOAD_GOOD, 0)
        assert "bat:" in r

    def test_uptime_shown(self, make_bot):
        device = {"model": "X", "ver": "1"}
        telemetry = {"total_up_time_secs": 7200}
        bot = make_bot(device_info=device, telemetry=telemetry)
        r = bot._cmd_info("", "Alice", PAYLOAD_GOOD, 0)
        assert "up:" in r

    def test_empty_device_no_crash(self, make_bot):
        bot = make_bot(device_info={}, telemetry={})
        r = bot._cmd_info("", "Alice", PAYLOAD_EMPTY, 0)
        assert "Alice" in r

    def test_fits_byte_limit(self, make_bot):
        device = {"model": "Heltec V3", "ver": "2.0.1", "adv_name": "LongNameNode"}
        telemetry = {
            "batt_milli_volts": 4100,
            "total_up_time_secs": 99999,
            "noise_floor": -110,
            "used_kb": 512, "total_kb": 1024,
        }
        bot = make_bot(device_info=device, telemetry=telemetry)
        r = bot._cmd_info("", "Alice", PAYLOAD_GOOD, 0)
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_stats
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdStats:
    def test_full_telemetry(self, make_bot):
        t = {"n_packets_recv": 100, "n_packets_sent": 50,
             "n_sent_flood": 30, "n_sent_direct": 20}
        bot = make_bot(telemetry=t)
        r = bot._cmd_stats("", "Alice", PAYLOAD_GOOD, 0)
        assert "rx:100" in r
        assert "tx:50" in r

    def test_no_telemetry_graceful(self, make_bot):
        bot = make_bot(telemetry={})
        r = bot._cmd_stats("", "Alice", PAYLOAD_GOOD, 0)
        assert "Alice" in r

    def test_fits_byte_limit(self, make_bot):
        t = {"n_packets_recv": 999, "n_packets_sent": 999,
             "n_sent_flood": 999, "n_sent_direct": 999,
             "err_events": 5, "n_direct_dups": 3, "n_flood_dups": 2,
             "total_air_time_secs": 86400}
        bot = make_bot(telemetry=t)
        r = bot._cmd_stats("", "Alice", PAYLOAD_GOOD, 0)
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_path
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdPath:
    def test_path_list(self, make_bot):
        payload = {"path": ["AAAAA", "BBBBB", "CCCCC"], "path_len": 2, "snr": 5}
        bot = make_bot()
        r = bot._cmd_path("", "Alice", payload, 0)
        assert "path:" in r

    def test_many_hops_warns(self, make_bot):
        payload = {"path_len": 6, "snr": 7, "path": ""}
        bot = make_bot()
        r = bot._cmd_path("", "Alice", payload, 0)
        assert "⚠️" in r

    def test_weak_signal_warns(self, make_bot):
        bot = make_bot()
        r = bot._cmd_path("", "Alice", PAYLOAD_WEAK, 0)
        assert "⚠️" in r

    def test_fits_byte_limit(self, make_bot):
        payload = {"path": ["A" * 8] * 6, "path_len": 5, "snr": -12}
        bot = make_bot()
        r = bot._cmd_path("", "A" * 15, payload, 0)
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_help
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdHelp:
    def test_contains_sender(self, make_bot):
        bot = make_bot()
        r = bot._cmd_help("", "Alice", PAYLOAD_GOOD, 0)
        assert "Alice" in r

    def test_contains_prefixes(self, make_bot):
        bot = make_bot()
        r = bot._cmd_help("", "Alice", PAYLOAD_GOOD, 0)
        assert "!b" in r or "!ai" in r

    def test_fits_byte_limit(self, make_bot):
        # help is intentionally multi-chunk — assert it fits in 5 packets
        bot = make_bot()
        r = bot._cmd_help("", "Alice", PAYLOAD_GOOD, 0)
        assert len(r.encode("utf-8")) <= BYTE_LIMIT * 5


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_weather / _cmd_news  (sync wrappers)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdWeatherNews:
    def test_weather_calls_web(self, make_bot, stub_web):
        bot = make_bot()
        r = bot._cmd_weather("London", "Alice", PAYLOAD_GOOD, 0)
        stub_web.weather.assert_called_once_with("London")
        assert "London" in r or "clear sky" in r

    def test_news_no_args(self, make_bot, stub_web):
        bot = make_bot()
        r = bot._cmd_news("", "Alice", PAYLOAD_GOOD, 0)
        # empty string → `"".strip() or None` → None
        stub_web.news.assert_called_once_with(None)
        assert "Headline" in r

    def test_weather_fits_byte_limit(self, make_bot):
        bot = make_bot()
        r = bot._cmd_weather("London", "Alice", PAYLOAD_GOOD, 0)
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# _cmd_monitor
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdMonitor:
    def test_enable_on_channel(self, make_bot):
        bot = make_bot()
        r = bot._cmd_monitor("on", "Alice", PAYLOAD_GOOD, channel=2)
        assert 2 in bot._monitored_channels
        assert "monitor" in r.lower() or "on" in r.lower()

    def test_disable_on_channel(self, make_bot):
        bot = make_bot()
        bot._monitored_channels.add(2)
        r = bot._cmd_monitor("off", "Alice", PAYLOAD_GOOD, channel=2)
        assert 2 not in bot._monitored_channels

    def test_no_channel_rejects(self, make_bot):
        bot = make_bot()
        r = bot._cmd_monitor("on", "Alice", PAYLOAD_GOOD, channel=None)
        assert "only works" in r or "channel" in r.lower()

    def test_unknown_arg_returns_help(self, make_bot):
        bot = make_bot()
        r = bot._cmd_monitor("maybe", "Alice", PAYLOAD_GOOD, channel=0)
        # Should explain valid args
        assert "on" in r or "off" in r or "usage" in r.lower()

    def test_fits_byte_limit(self, make_bot):
        bot = make_bot()
        r = bot._cmd_monitor("on", "Alice", PAYLOAD_GOOD, channel=0)
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# record_message / channel history
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecordMessage:
    def test_records_to_channel(self, make_bot):
        bot = make_bot()
        bot.record_message(0, "Alice", "hello", {"snr": 5})
        assert 0 in bot._chan_history
        assert bot._chan_history[0][-1]["sender"] == "Alice"

    def test_none_channel_skipped(self, make_bot):
        bot = make_bot()
        bot.record_message(None, "Alice", "hello", {})
        assert len(bot._chan_history) == 0

    def test_channel_history_maxlen_respected(self, make_bot):
        bot = make_bot(extra_cfg={"channel_history_len": 5})
        for i in range(10):
            bot.record_message(0, f"user{i}", f"msg{i}", {})
        assert len(bot._chan_history[0]) == 5

    def test_multiple_channels_independent(self, make_bot):
        bot = make_bot()
        bot.record_message(0, "Alice", "ch0", {})
        bot.record_message(1, "Bob", "ch1", {})
        assert bot._chan_history[0][-1]["sender"] == "Alice"
        assert bot._chan_history[1][-1]["sender"] == "Bob"


# ═══════════════════════════════════════════════════════════════════════════════
# async _cmd_snr
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdSnrAsync:
    def test_snr_calls_llm_analyze(self, make_bot, stub_llm):
        bot = make_bot()
        stub_llm.analyze.return_value = "signal analysis result"
        r = _run(bot._cmd_snr("", "Alice", PAYLOAD_GOOD, 0))
        assert stub_llm.analyze.called
        assert "Alice" in r

    def test_snr_fits_byte_limit(self, make_bot):
        bot = make_bot()
        r = _run(bot._cmd_snr("", "Alice", PAYLOAD_GOOD, 0))
        assert _fits(r)

    def test_snr_weak_signal_in_prompt(self, make_bot, stub_llm):
        bot = make_bot()
        _run(bot._cmd_snr("", "Alice", PAYLOAD_WEAK, 0))
        call_args = stub_llm.analyze.call_args[0][0]
        assert "critical" in call_args or "-15" in call_args


# ═══════════════════════════════════════════════════════════════════════════════
# async _cmd_search
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdSearchAsync:
    def test_search_calls_web(self, make_bot, stub_web):
        bot = make_bot()
        r = _run(bot._cmd_search("bitcoin price", "Alice", PAYLOAD_GOOD, 0))
        stub_web.search.assert_called_once_with("bitcoin price")
        assert "Alice" in r

    def test_empty_query_returns_hint(self, make_bot):
        bot = make_bot()
        r = _run(bot._cmd_search("", "Alice", PAYLOAD_GOOD, 0))
        assert "provide" in r.lower() or "search" in r.lower()

    def test_fits_byte_limit(self, make_bot):
        bot = make_bot()
        r = _run(bot._cmd_search("test", "Alice", PAYLOAD_GOOD, 0))
        assert _fits(r)


# ═══════════════════════════════════════════════════════════════════════════════
# all commands — response byte limit sweep
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllCommandsByteLimit:
    """Each sync command must produce a reply that fits in BYTE_LIMIT."""

    LONG_SENDER = "VeryLongCallsign2049"

    @pytest.fixture
    def bot(self, make_bot):
        d = {"model": "Heltec V3", "ver": "2.0.0", "adv_name": self.LONG_SENDER}
        t = {"batt_milli_volts": 3800, "total_up_time_secs": 12345,
             "n_packets_recv": 9999, "n_packets_sent": 9999}
        return make_bot(device_info=d, telemetry=t)

    def _check(self, result: str):
        assert isinstance(result, str)
        assert _fits(result), f"Response too long ({len(result.encode())} bytes): {result!r}"

    def test_ping(self, bot):
        self._check(bot._cmd_ping("", self.LONG_SENDER, PAYLOAD_GOOD, 0))

    def test_test(self, bot):
        self._check(bot._cmd_test("", self.LONG_SENDER, PAYLOAD_GOOD, 0))

    def test_info(self, bot):
        self._check(bot._cmd_info("", self.LONG_SENDER, PAYLOAD_GOOD, 0))

    def test_stats(self, bot):
        self._check(bot._cmd_stats("", self.LONG_SENDER, PAYLOAD_GOOD, 0))

    def test_path(self, bot):
        self._check(bot._cmd_path("", self.LONG_SENDER, PAYLOAD_GOOD, 0))

    def test_help(self, bot):
        # help is multi-chunk by design — just check it produces content
        r = bot._cmd_help("", self.LONG_SENDER, PAYLOAD_GOOD, 0)
        assert isinstance(r, str) and len(r) > 0

    def test_weather(self, bot):
        self._check(bot._cmd_weather("London", self.LONG_SENDER, PAYLOAD_GOOD, 0))

    def test_news(self, bot):
        self._check(bot._cmd_news("", self.LONG_SENDER, PAYLOAD_GOOD, 0))
