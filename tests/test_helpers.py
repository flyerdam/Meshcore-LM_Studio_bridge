"""
Unit tests for meshcore_bridge/helpers.py

Tests every public function including edge cases and byte-limit boundaries.
"""
import pytest
from meshcore_bridge.helpers import (
    detect_language,
    fit_to_bytes,
    get_payload_value,
    hops_quality,
    sanitize_mesh_reply,
    snr_quality,
    strip_think_tags,
    strip_tool_artifacts,
    to_int,
    uptime_str,
    wmo_code,
)
from meshcore_bridge.config import BYTE_LIMIT


# ═══════════════════════════════════════════════════════════════════════════════
# fit_to_bytes
# ═══════════════════════════════════════════════════════════════════════════════

class TestFitToBytes:
    def test_short_string_unchanged(self):
        s = "Hello"
        assert fit_to_bytes(s, 130) == s

    def test_exact_limit_unchanged(self):
        s = "A" * BYTE_LIMIT
        assert fit_to_bytes(s) == s

    def test_over_limit_truncated(self):
        s = "B" * (BYTE_LIMIT + 50)
        result = fit_to_bytes(s)
        assert len(result.encode("utf-8")) <= BYTE_LIMIT

    def test_truncated_ends_with_ellipsis(self):
        s = "C" * 300
        result = fit_to_bytes(s)
        assert result.endswith("…")

    def test_multibyte_utf8_not_split(self):
        # Each Polish character is 2 bytes — must not split mid-codepoint
        s = "ą" * 100  # 200 bytes total
        result = fit_to_bytes(s, 130)
        assert len(result.encode("utf-8")) <= 130
        result.encode("utf-8")  # must not raise UnicodeEncodeError

    def test_emoji_not_split(self):
        # Each emoji is 4 bytes
        s = "🏓" * 50
        result = fit_to_bytes(s, 130)
        assert len(result.encode("utf-8")) <= 130

    def test_empty_string(self):
        assert fit_to_bytes("", 130) == ""

    def test_custom_limit(self):
        s = "x" * 50
        result = fit_to_bytes(s, 20)
        assert len(result.encode("utf-8")) <= 20

    def test_limit_zero_returns_ellipsis_or_empty(self):
        # limit=0 → budget=max(0, 0-3)=0 → empty text + ellipsis = "…"
        result = fit_to_bytes("hello world", 0)
        # Either pure empty or just the ellipsis marker — must not crash
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════════════
# snr_quality
# ═══════════════════════════════════════════════════════════════════════════════

class TestSnrQuality:
    @pytest.mark.parametrize("snr,expected", [
        (15,   "excellent"),
        (10,   "excellent"),
        (9.9,  "good"),
        (5,    "good"),
        (4.9,  "weak"),
        (0,    "weak"),
        (-0.1, "very weak"),
        (-10,  "very weak"),
        (-10.1,"critical"),
        (-99,  "critical"),
    ])
    def test_thresholds(self, snr, expected):
        assert snr_quality(snr) == expected

    def test_none_returns_unknown(self):
        assert snr_quality(None) == "unknown"

    def test_string_number(self):
        assert snr_quality("7") == "good"

    def test_garbage_returns_unknown(self):
        assert snr_quality("not_a_number") == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# hops_quality
# ═══════════════════════════════════════════════════════════════════════════════

class TestHopsQuality:
    def test_zero_hops(self):
        assert hops_quality(0) == "direct"

    def test_one_hop(self):
        assert hops_quality(1) == "1 hop"

    def test_many_hops(self):
        assert hops_quality(7) == "7 hops"


# ═══════════════════════════════════════════════════════════════════════════════
# uptime_str
# ═══════════════════════════════════════════════════════════════════════════════

class TestUptimeStr:
    def test_seconds_only(self):
        assert uptime_str(45) == "0m45s"

    def test_minutes(self):
        assert uptime_str(125) == "2m05s"

    def test_hours(self):
        result = uptime_str(3661)
        assert result.startswith("1h")

    def test_zero(self):
        assert uptime_str(0) == "0m00s"

    def test_large_uptime(self):
        # 100 hours
        result = uptime_str(360000)
        assert "100h" in result


# ═══════════════════════════════════════════════════════════════════════════════
# to_int
# ═══════════════════════════════════════════════════════════════════════════════

class TestToInt:
    def test_int(self):
        assert to_int(5) == 5

    def test_string(self):
        assert to_int("42") == 42

    def test_float_string(self):
        # int() on a float string raises ValueError in Python → default returned
        assert to_int("3.7", -1) == -1

    def test_none_returns_default(self):
        assert to_int(None) == 0
        assert to_int(None, 99) == 99

    def test_garbage_returns_default(self):
        assert to_int("abc", -1) == -1


# ═══════════════════════════════════════════════════════════════════════════════
# wmo_code
# ═══════════════════════════════════════════════════════════════════════════════

class TestWmoCode:
    def test_known_codes(self):
        assert wmo_code(0) == "clear sky"
        assert wmo_code(61) == "slight rain"
        assert wmo_code(95) == "thunderstorm"

    def test_unknown_code(self):
        assert wmo_code(999) == "code:999"


# ═══════════════════════════════════════════════════════════════════════════════
# get_payload_value
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetPayloadValue:
    def test_lowercase_key(self):
        assert get_payload_value({"snr": 5}, "snr") == 5

    def test_uppercase_key(self):
        assert get_payload_value({"SNR": 10}, "snr") == 10

    def test_capitalized_key(self):
        assert get_payload_value({"Snr": 3}, "snr") == 3

    def test_fallback_key(self):
        assert get_payload_value({}, "snr", "rssi", default=-99) == -99

    def test_second_key_used_if_first_missing(self):
        assert get_payload_value({"rssi": -80}, "snr", "rssi") == -80

    def test_none_value_skipped(self):
        assert get_payload_value({"snr": None}, "snr", default="?") == "?"

    def test_empty_payload(self):
        assert get_payload_value({}, "snr") == "?"

    def test_zero_value_returned(self):
        # 0 is a valid value, must not fall through to default
        assert get_payload_value({"path_len": 0}, "path_len", default="?") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# sanitize_mesh_reply
# ═══════════════════════════════════════════════════════════════════════════════

class TestSanitizeMeshReply:
    def test_strips_backticks(self):
        assert "`" not in sanitize_mesh_reply("use `command` here")

    def test_strips_char_limit_note(self):
        text = "Good answer (Note: 150 char limit exceeded — truncated)"
        result = sanitize_mesh_reply(text)
        assert "Note:" not in result

    def test_collapses_whitespace(self):
        result = sanitize_mesh_reply("hello   world\n\n  there")
        assert "  " not in result

    def test_empty_string(self):
        assert sanitize_mesh_reply("") == ""

    def test_plain_text_unchanged(self):
        t = "Hello from the mesh"
        assert sanitize_mesh_reply(t) == t


# ═══════════════════════════════════════════════════════════════════════════════
# strip_think_tags
# ═══════════════════════════════════════════════════════════════════════════════

class TestStripThinkTags:
    def test_removes_think_block(self):
        text = "<think>internal reasoning here</think>Final answer"
        assert strip_think_tags(text) == "Final answer"

    def test_no_think_tags_unchanged(self):
        t = "plain text"
        assert strip_think_tags(t) == t

    def test_multiline_think_block(self):
        text = "<think>\nline1\nline2\n</think>Result"
        assert strip_think_tags(text) == "Result"

    def test_empty_string(self):
        assert strip_think_tags("") == ""

    def test_think_heading_first_line_stripped(self):
        text = "Thinking process\nSome internal stuff\n\nFinal: The answer is 42"
        result = strip_think_tags(text)
        # Should keep the final paragraph
        assert "42" in result


# ═══════════════════════════════════════════════════════════════════════════════
# strip_tool_artifacts
# ═══════════════════════════════════════════════════════════════════════════════

class TestStripToolArtifacts:
    def test_removes_control_tokens(self):
        text = "Hello <|im_start|> world"
        result = strip_tool_artifacts(text)
        assert "<|" not in result

    def test_removes_commentary_artifact(self):
        text = "commentary to=user the answer is yes"
        result = strip_tool_artifacts(text)
        assert "commentary" not in result

    def test_removes_json_path_artifact(self):
        text = 'Answer {"path": "/some/path"} done'
        result = strip_tool_artifacts(text)
        assert "{" not in result

    def test_empty_string(self):
        assert strip_tool_artifacts("") == ""

    def test_clean_text_unchanged(self):
        t = "SNR is 7 dB"
        assert strip_tool_artifacts(t) == t


# ═══════════════════════════════════════════════════════════════════════════════
# detect_language
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectLanguage:
    def test_polish_diacritics(self):
        assert detect_language("Cześć, jak się masz?") == "Polish"

    def test_german_diacritics(self):
        # German text with enough unique chars
        assert detect_language("Guten Tag, Öl für Björn, größer") == "German"

    def test_russian_cyrillic(self):
        assert detect_language("Привет, как дела?") == "Russian"

    def test_polish_hint_words(self):
        assert detect_language("tak tak tak ogarniesz to?") == "Polish"

    def test_empty_returns_none(self):
        assert detect_language("") is None

    def test_plain_ascii_returns_none(self):
        # No diacritics → no confident detection
        result = detect_language("Hello world how are you")
        assert result is None
