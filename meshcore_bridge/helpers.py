"""
Utility / helper functions shared across modules.
"""

import re

from meshcore_bridge.config import BYTE_LIMIT


# ── Language detection ───────────────────────────────────────────────────────
# Lightweight heuristic — no external deps. Detects by unique script/char sets.
_LANG_PROFILES: list[tuple[str, set[str]]] = [
    ("Polish",     set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")),
    ("German",     set("äöüßÄÖÜ")),
    ("French",     set("àâæçèéêëîïôœùûüÿÀÂÆÇÈÉÊËÎÏÔŒÙÛÜŸ")),
    ("Spanish",    set("áéíóúüñÁÉÍÓÚÜÑ¿¡")),
    ("Portuguese", set("ãõáéíóúâêôàçÃÕÁÉÍÓÚÂÊÔÀÇ")),
    ("Italian",    set("àèéìîóùÀÈÉÌÎÓÙ")),
    ("Russian",    set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")),
]

_POLISH_HINTS = {
    "tak", "nie", "po", "polsku", "fajny", "gosc", "gość", "krotki", "krótki",
    "wiersz", "botach", "potrzebuje", "potrzebuję", "napisal", "napisał",
    "ilu", "masz", "kolegow", "kolegów", "ogarniesz", "asystent", "czemu",
    "jestes", "jesteś", "tutaj", "halo", "odbior", "odbiór", "dziala", "działa",
}


def detect_language(text: str) -> str | None:
    """Return best-guess language name, or None if confidence is weak."""
    if not text:
        return None
    scores: dict[str, int] = {}
    for lang, chars in _LANG_PROFILES:
        score = sum(1 for c in text if c in chars)
        if score:
            scores[lang] = score
    lowered = text.lower()
    tokens = set(re.findall(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+", lowered))
    polish_hits = len(tokens & _POLISH_HINTS)
    if polish_hits >= 2:
        scores["Polish"] = scores.get("Polish", 0) + polish_hits
    if not scores:
        return None
    return max(scores, key=lambda k: scores[k])


def sanitize_mesh_reply(text: str) -> str:
    """Remove formatting/noise that should never be sent over mesh."""
    if not text:
        return ""
    cleaned = text.strip()
    cleaned = re.sub(r"\(Note:\s*\d+\s*char\s*limit\s*exceeded[^)]*\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def get_payload_value(payload: dict, *keys, default="?"):
    """
    Fetches a value from the payload trying different key variants.
    MeshCore returns e.g., 'SNR' instead of 'snr' — we handle both.
    """
    for key in keys:
        for variant in (key, key.upper(), key.lower(), key.capitalize()):
            if variant in payload and payload[variant] is not None:
                return payload[variant]
    return default


def strip_think_tags(text: str) -> str:
    """Remove LLM 'thinking' artifacts from the response."""
    if "<think>" in text and "</think>" in text:
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return cleaned if cleaned else text.strip()
    think_pat = re.compile(
        r"^(thinking(\s+process)?|let me think|internal monologue|"
        r"drafting|draft\s+\d|idea\s+\d|step\s+\d|analyzing|"
        r"reasoning|train of thought|thinking|analyze)",
        re.IGNORECASE,
    )
    first_line = text.split("\n")[0].strip()
    if think_pat.match(first_line):
        parts = [p.strip() for p in re.split(r"\n{2,}", text.strip()) if p.strip()]
        if len(parts) > 1:
            last = parts[-1]
            if think_pat.match(last.split("\n")[0]):
                sentences = re.split(r"(?<=[.!?])\s+", last)
                return sentences[-1].strip() if sentences else last
            return last
    final = re.search(
        r"\*\*(final output|final answer|final response|output)[:\s*]*\*?\*?\n+(.*)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if final:
        return final.group(2).strip()
    return text.strip()


def strip_tool_artifacts(text: str) -> str:
    """Remove tool-call/control-token artifacts leaked by some models."""
    if not text:
        return ""

    cleaned = text

    # Remove OpenAI-style control token fragments.
    cleaned = re.sub(r"<\|[^|]+\|>", "", cleaned)

    # Remove common tool-call envelopes seen in bad model outputs.
    cleaned = re.sub(r"commentary\s+to=[^\n]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\{\s*\"path\"\s*:[^\n]*\}", "", cleaned)

    # Collapse whitespace leftovers.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def fit_to_bytes(text: str, limit: int = BYTE_LIMIT) -> str:
    """Truncate *text* so its UTF-8 encoding fits within *limit* bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # Reserve 3 bytes for the UTF-8 ellipsis (U+2026 = \xe2\x80\xa6)
    ellipsis_bytes = "\u2026".encode("utf-8")  # always 3 bytes
    budget = max(0, limit - len(ellipsis_bytes))
    truncated = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return truncated + "\u2026"


def uptime_str(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h > 0 else f"{m}m{s:02d}s"


def snr_quality(snr) -> str:
    try:
        snr = float(snr)
    except (TypeError, ValueError):
        return "unknown"
    if snr >= 10:  return "excellent"
    if snr >= 5:   return "good"
    if snr >= 0:   return "weak"
    if snr >= -10: return "very weak"
    return "critical"


def hops_quality(hops: int) -> str:
    if hops == 0:    return "direct"
    if hops == 1:    return "1 hop"
    return f"{hops} hops"


def wmo_code(code: int) -> str:
    codes = {
        0: "clear sky", 1: "mainly clear", 2: "partly cloudy",
        3: "overcast", 45: "fog", 48: "depositing rime fog",
        51: "drizzle", 53: "moderate drizzle", 55: "dense drizzle",
        61: "slight rain", 63: "moderate rain", 65: "heavy rain",
        71: "slight snow", 73: "moderate snow", 75: "heavy snow",
        80: "rain showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    }
    return codes.get(code, f"code:{code}")


def to_int(val, default=0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
