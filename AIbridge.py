#!/usr/bin/env python3
"""
MeshCore USB Companion <-> LM Studio Bridge + Bot
==================================================
Requires Python 3.10+

Installation:
    pip install meshcore requests

Execution:
    python AIbridge.py

Overriding parameters:
    python AIbridge.py --port COM4
    python AIbridge.py --model "other-model"
    python AIbridge.py --bot-prefix "!f" --ai-prefix "!q"
    python AIbridge.py --listen-channels 2 --reply-channel 2
    python AIbridge.py --news-key YOUR_NEWSAPI_KEY
    python AIbridge.py --telemetry-interval 60

Bot commands (default prefix !bot):
    !bot ping           → Pong + SNR + hops
    !bot test           → Ack + connection parameters
    !bot info           → firmware, model, uptime, NF, battery
    !bot stats          → statistics for rx/tx/flood/direct packets
    !bot path           → routing path + quality assessment
    !bot snr            → SNR analysis by AI
    !bot weather <city> → current weather (Open-Meteo geocoding)
    !bot news [topic]   → headlines (requires --news-key)
    !bot search <what>  → DuckDuckGo instant answer
    !bot channel        → AI analyzes message history from the channel
    !bot help           → command list

LLM commands (default prefix !ai):
    !ai <question>      → query to the local LLM
    !ai reset           → clear conversation history
"""

import asyncio
import inspect
import requests
import logging
import sys
import argparse
import re
from datetime import datetime
from collections import deque

from meshcore import MeshCore, EventType

# ─── Logger ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("AIbridge.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BYTE_LIMIT = 130  # 141B mesh limit – margin for UTF-8 characters


# ─── Default Configuration ─────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "serial_port":          "COM3",
    "baud_rate":            115200,
    "lm_url":               "http://localhost:1234/v1/chat/completions",
    "model":                "google/gemma-3-12b",
    "system_prompt": (
        "You are an AI assistant named 'flyer AI' operating in a LoRa mesh network via MeshCore. "
        "You converse with radio operators via the LoRa protocol - messages have a 141-byte limit. "
        "\n\nBOT CAPABILITIES (prefix !bot):\n"
        "!bot ping – connection test, returns SNR and hops\n"
        "!bot test – full connection parameters\n"
        "!bot info – node info: firmware, model, uptime, battery\n"
        "!bot stats – packet statistics rx/tx/flood/direct\n"
        "!bot path – routing path and quality assessment\n"
        "!bot snr – signal quality analysis by AI\n"
        "!bot weather <city> – current weather from Open-Meteo\n"
        "!bot news [topic] – NewsAPI headlines\n"
        "!bot search <what> – DuckDuckGo search\n"
        "!bot channel – SNR analysis of all stations on the channel\n"
        "!bot monitor on/off – SNR monitoring with automatic warnings\n"
        "!bot help – command list\n"
        "\nAI (prefix !ai): !ai <question> | !ai reset\n"
        "\nRESPONSE RULES: "
        "1. Only the final answer - zero train of thought, zero headers. "
        "2. Max 300 characters - you are in a mesh network, long answers are split into packets. "
        "3. No markdown, asterisks, or lists. Plain text only. "
        "4. Write in English, unless someone speaks in another language. "
        "5. Be concise and to the point. Like a knight - helpful and direct. "
        "6. If you see the channel context below - you can refer to it."
    ),
    "channel_context_msgs": 20,    # how many recent channel messages to inject into AI context (0 = disabled)
    "max_chunks":           5,     # max number of mesh packets for one response
    "history_len":          20,    # how many messages AI remembers per conversation/user
    "ai_prefix":            "!ai", # trigger for LLM query
    "bot_prefix":           "!b",  # trigger for bot commands (ping, test, info etc.)
    "reply_delay_s":        0.5,   # delay before replying (seconds)
    "listen_channels":      None,  # None = all channels; e.g., [0, 2] = only those channels
    "reply_channel":        None,  # None = reply on the same channel as the question; e.g., 2
    "poll_interval_s":      0.5,   # how often to poll MeshCore for new messages (seconds)
    "telemetry_interval_s": 300,   # how often to poll the radio for telemetry (battery, uptime etc.)
    "news_api_key":         None,  # NewsAPI key (newsapi.org, free 100 req/day); None = DDG only
    "news_country":         "us",  # country for top headlines in NewsAPI
    "channel_history_len":  50,    # how many recent channel messages to keep in memory (for !bot channel and context)
    "monitor_reminder_s":   600,   # how often to send automatic SNR reports on monitored channels
}


# ─── Helpers ───────────────────────────────────────────────────────────────
def _p(payload: dict, *keys, default="?"):
    """
    Fetches a value from the payload trying different key variants.
    MeshCore returns e.g., 'SNR' instead of 'snr' - we handle both.
    """
    for key in keys:
        for variant in (key, key.upper(), key.lower(), key.capitalize()):
            if variant in payload and payload[variant] is not None:
                return payload[variant]
    return default


def _strip_think_tags(text: str) -> str:
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
        text, re.IGNORECASE | re.DOTALL
    )
    if final:
        return final.group(2).strip()
    return text.strip()


def _fit_to_bytes(text: str, limit: int = BYTE_LIMIT) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip() + "…"


def _uptime_str(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h > 0 else f"{m}m{s:02d}s"


def _snr_quality(snr) -> str:
    try:
        snr = float(snr)
    except (TypeError, ValueError):
        return "unknown"
    if snr >= 10:  return "excellent"
    if snr >= 5:   return "good"
    if snr >= 0:   return "weak"
    if snr >= -10: return "very weak"
    return "critical"


def _hops_quality(hops: int) -> str:
    if hops == 0:    return "direct"
    if hops == 1:    return "1 hop"
    if hops <= 3:    return f"{hops} hops"
    return f"{hops} hops ⚠️"


def _wmo_code(code: int) -> str:
    codes = {
        0: "clear sky", 1: "mainly clear", 2: "partly cloudy",
        3: "overcast", 45: "fog", 48: "depositing rime fog",
        51: "drizzle", 53: "moderate drizzle", 55: "dense drizzle",
        61: "slight rain", 63: "moderate rain", 65: "heavy rain",
        71: "slight snow", 73: "moderate snow", 75: "heavy snow",
        80: "rain showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    }
    return codes.get(code, f"code:{code}")


def _to_int(val, default=0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ─── Web Client ──────────────────────────────────────────────────────────────
class WebSearch:

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def weather(self, city: str | None = None) -> str:
        if not city:
            bp = self.cfg.get("bot_prefix", "!bot")
            return f"provide city: {bp} weather London"
        try:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en"},
                timeout=8,
            )
            results = geo.json().get("results")
            if not results:
                return f"Not found: {city}"
            r    = results[0]
            lat, lon = r["latitude"], r["longitude"]
            name = r.get("name", city)
            cc   = r.get("country_code", "")
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current":  "temperature_2m,precipitation,wind_speed_10m,weathercode",
                    "wind_speed_unit": "ms", "timezone": "auto",
                },
                timeout=8,
            )
            cur  = resp.json()["current"]
            temp = cur.get("temperature_2m", "?")
            prec = cur.get("precipitation", 0)
            wind = cur.get("wind_speed_10m", "?")
            desc = _wmo_code(cur.get("weathercode", 0))
            rain = f" rain:{prec}mm" if prec > 0 else ""
            return f"{name}({cc}): {desc} {temp}°C wind:{wind}m/s{rain}"
        except Exception as e:
            log.error("Weather error: %s", e)
            return "Error fetching weather"

    def news(self, query: str | None = None) -> str:
        key = self.cfg.get("news_api_key")
        if not key:
            return "No NewsAPI key. Use --news-key (newsapi.org, free)"
        try:
            if query:
                url, params = "https://newsapi.org/v2/everything", {
                    "q": query, "pageSize": 3, "sortBy": "publishedAt",
                    "language": "en", "apiKey": key,
                }
            else:
                url, params = "https://newsapi.org/v2/top-headlines", {
                    "country": self.cfg.get("news_country", "us"),
                    "pageSize": 3, "apiKey": key,
                }
            arts = requests.get(url, params=params, timeout=10).json().get("articles", [])
            if not arts:
                return "No news"
            return " | ".join(a["title"].split(" - ")[0][:70] for a in arts[:3])
        except Exception as e:
            log.error("NewsAPI error: %s", e)
            return "Error fetching news"

    def search(self, query: str) -> str:
        if not query.strip():
            bp = self.cfg.get("bot_prefix", "!bot")
            return f"provide a query: {bp} search bitcoin"
        try:
            data = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "no_redirect": 1},
                timeout=10,
            ).json()
            abstract = data.get("AbstractText", "").strip()
            if abstract:
                return abstract[:250]
            answer = data.get("Answer", "").strip()
            if answer:
                return answer[:250]
            related = [
                item["Text"][:80]
                for item in data.get("RelatedTopics", [])[:2]
                if isinstance(item, dict) and "Text" in item
            ]
            return " | ".join(related) if related else f"No results for: {query}"
        except Exception as e:
            log.error("DDG error: %s", e)
            return "Search error"


# ─── LM Studio Client ──────────────────────────────────────────────────────
class LMStudioClient:

    def __init__(self, url: str, model: str, system_prompt: str, history_len: int = 5):
        self.url           = url
        self.model         = model
        self.system_prompt = system_prompt
        self.history_len   = history_len
        self._histories: dict[str, deque] = {}

    def _history(self, sender: str) -> deque:
        if sender not in self._histories:
            self._histories[sender] = deque(maxlen=self.history_len * 2)
        return self._histories[sender]

    def ask(self, sender: str, question: str,
            channel_context: list[dict] | None = None) -> str:
        return self._call(sender, question,
                          save_history=True, channel_context=channel_context)

    def analyze(self, prompt: str) -> str:
        return self._call("__analysis__", prompt, save_history=False)

    def _call(self, sender: str, question: str,
              save_history: bool = True,
              channel_context: list[dict] | None = None) -> str:
        hist = self._history(sender)

        messages = [{"role": "system", "content": self.system_prompt}]

        # Inject channel context as "user" messages before the conversation history
        # This allows AI to "see" recent channel messages
        if channel_context:
            ctx_lines = "\n".join(
                f"{m['sender']}: {m['text']}" for m in channel_context
            )
            messages.append({
                "role": "user",
                "content": (
                    f"[Channel context – recent messages before question]\n"
                    f"{ctx_lines}"
                )
            })
            messages.append({
                "role": "assistant",
                "content": "I understand the channel context. Awaiting question."
            })

        if save_history:
            hist.append({"role": "user", "content": question})
            messages.extend(list(hist))
        else:
            messages.append({"role": "user", "content": question})
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model, "messages": messages,
                    "max_tokens": 300, "temperature": 0.7, "stream": False,
                },
                timeout=60,
            )
            if resp.status_code != 200:
                log.error("LM Studio HTTP %d: %s", resp.status_code, resp.text[:200])
                return f"[HTTP Error {resp.status_code}]"
            content = resp.json()["choices"][0]["message"]["content"].strip()
            content = _strip_think_tags(content)
            if save_history:
                hist.append({"role": "assistant", "content": content})
            return content
        except requests.exceptions.ConnectionError:
            return "[LM Studio unavailable]"
        except requests.exceptions.Timeout:
            return "[Timeout – model did not respond]"
        except Exception as e:
            log.exception("LLM Error")
            return f"[Error: {e}]"

    def clear_history(self, sender: str):
        self._histories.pop(sender, None)


# ─── Bot Commands ──────────────────────────────────────────────────────────
class BotCommands:

    CMDS = {
        "ping":    "_cmd_ping",
        "test":    "_cmd_test",
        "info":    "_cmd_info",
        "status":  "_cmd_info",
        "stats":   "_cmd_stats",
        "path":    "_cmd_path",
        "snr":     "_cmd_snr",
        "weather": "_cmd_weather",
        "news":    "_cmd_news",
        "search":  "_cmd_search",
        "channel": "_cmd_chan_analysis",
        "channels":"_cmd_channels",
        "reset":   "_cmd_reset_paths",
        "monitor": "_cmd_monitor",
        "help":    "_cmd_help",
    }

    def __init__(self, device_info: dict, cfg: dict,
                 llm: LMStudioClient, web: WebSearch, telemetry: dict, mc):
        self.device_info    = device_info
        self.cfg            = cfg
        self.llm            = llm
        self.web            = web
        self.telemetry      = telemetry
        self.mc             = mc   # reference to MeshCore – needed for get_channels, reset_path
        self._chan_history: dict[int, deque] = {}
        # Channels with SNR monitoring enabled: {channel_idx}
        self._monitored_channels: set[int] = set()

    def record_message(self, channel: int | None, sender: str, text: str, payload: dict):
        if channel is None:
            return
        if channel not in self._chan_history:
            self._chan_history[channel] = deque(
                maxlen=self.cfg.get("channel_history_len", 20)
            )
        self._chan_history[channel].append({
            "sender": sender,
            "text":   text,
            "snr":    _p(payload, "snr", default="?"),
            "hops":   _p(payload, "path_len", default=0),
            "ts":     _p(payload, "sender_timestamp", default=0),
        })

    def match(self, body: str) -> tuple[str | None, str]:
        parts = body.strip().split(None, 1)
        if not parts:
            return None, body
        cmd  = self.CMDS.get(parts[0].lower())
        rest = parts[1] if len(parts) > 1 else ""
        return cmd, rest

    async def handle(self, cmd_name: str, args: str, sender: str,
                     payload: dict, channel: int | None) -> str:
        method = getattr(self, cmd_name, None)
        if method:
            if inspect.iscoroutinefunction(method):
                return await method(args, sender, payload, channel)
            return method(args, sender, payload, channel)
        return ""

    # ── Synchronous ─────────────────────────────────────────────────────────
    def _cmd_ping(self, args, sender, payload, channel) -> str:
        snr  = _p(payload, "snr")
        hops = _to_int(_p(payload, "path_len", default=0))
        q    = _snr_quality(snr)
        return f"@{sender} Pong! 🏓 SNR:{snr}({q}) {_hops_quality(hops)}"

    def _cmd_test(self, args, sender, payload, channel) -> str:
        snr  = _p(payload, "snr")
        hops = _to_int(_p(payload, "path_len", default=0))
        ts   = _to_int(_p(payload, "sender_timestamp", default=0))
        t    = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
        q    = _snr_quality(snr)
        return f"@{sender} Ack! ✅ SNR:{snr}({q}) {_hops_quality(hops)} {t}"

    def _cmd_info(self, args, sender, payload, channel) -> str:
        d = self.device_info or {}
        t = self.telemetry   or {}

        mdl = d.get("model", "?")
        ver = d.get("ver", "?")

        # uptime – total_up_time_secs field if it exists
        upt = t.get("total_up_time_secs") or d.get("total_up_time_secs")
        upt_str = _uptime_str(_to_int(upt)) if upt else "?"

        # noise floor
        nfl     = t.get("noise_floor") or d.get("noise_floor")
        nfl_str = f" NF:{nfl}dBm" if nfl is not None else ""

        # battery – "level" is in per mille (0-1000), batt_milli_volts is in mV
        batt_str = ""
        level    = t.get("level") or d.get("level")
        batt_mv  = t.get("batt_milli_volts") or d.get("batt_milli_volts")
        if batt_mv:
            pct      = max(0, min(100, int((_to_int(batt_mv) - 3000) / 12)))
            batt_str = f" bat:{pct}%"
        elif level is not None:
            # level in per mille (0-1000 = 0-100%)
            pct      = _to_int(level) // 10
            batt_str = f" bat:{pct}%"

        # memory
        used = t.get("used_kb") or d.get("used_kb")
        total= t.get("total_kb") or d.get("total_kb")
        mem_str = f" mem:{used}/{total}kB" if used is not None and total else ""

        # SNR from the current message or telemetry
        snr  = _p(payload, "snr", default=None) or t.get("last_snr")
        rssi = _p(payload, "rssi", default=None) or t.get("last_rssi")
        rf   = (f" SNR:{snr}"    if snr  is not None else "") + \
               (f" RSSI:{rssi}"  if rssi is not None else "")

        return f"@{sender} {mdl} {ver} up:{upt_str}{nfl_str}{batt_str}{mem_str}{rf}"

    def _cmd_stats(self, args, sender, payload, channel) -> str:
        t     = self.telemetry or {}
        recv  = t.get("n_packets_recv",  "?")
        sent  = t.get("n_packets_sent",  "?")
        flood = t.get("n_sent_flood",    "?")
        direct= t.get("n_sent_direct",   "?")
        errs  = t.get("err_events",      0)
        dups  = _to_int(t.get("n_direct_dups", 0)) + _to_int(t.get("n_flood_dups", 0))
        air   = t.get("total_air_time_secs")
        air_s = f" air:{_uptime_str(_to_int(air))}" if air else ""
        # If no statistical data - show what we have from telemetry
        if recv == "?" and sent == "?":
            available = {k: v for k, v in t.items()
                        if k not in ("model", "ver", "fw_build", "fw ver")}
            if available:
                return f"@{sender} telemetry: {available}"
            return f"@{sender} no statistical data (firmware does not provide)"
        return f"@{sender} rx:{recv} tx:{sent} flood:{flood} dir:{direct} err:{errs} dup:{dups}{air_s}"

    def _cmd_path(self, args, sender, payload, channel) -> str:
        path = _p(payload, "path", "route", default="")
        if isinstance(path, list):
            path = ">".join(str(p)[:4] for p in path)
        hops = _to_int(_p(payload, "path_len", default=0))
        snr  = _p(payload, "snr")
        if not path or path == "?":
            path = _hops_quality(hops)
        q = _snr_quality(snr)
        if hops > 4:
            warn = " ⚠️ long route"
        elif q in ("very weak", "critical"):
            warn = " ⚠️ weak signal"
        else:
            warn = " ✅"
        return f"@{sender} path:{path} SNR:{snr}{warn}"

    def _cmd_weather(self, args, sender, payload, channel) -> str:
        return f"@{sender} {self.web.weather(args.strip() or None)}"

    def _cmd_news(self, args, sender, payload, channel) -> str:
        return f"@{sender} {self.web.news(args.strip() or None)}"

    async def _cmd_channels(self, args, sender, payload, channel) -> str:
        """Fetches the list of configured channels from the device."""
        try:
            result = await asyncio.wait_for(
                self.mc.commands.get_channels(), timeout=5.0
            )
            if result.type == EventType.ERROR:
                return f"@{sender} error fetching channels: {result.payload}"
            channels = result.payload
            if not channels:
                return f"@{sender} no configured channels"
            # channels is a dict or list - handle both formats
            if isinstance(channels, dict):
                items = channels.items()
            else:
                items = enumerate(channels)
            parts = []
            for idx, ch in items:
                name = ch.get("name", "") if isinstance(ch, dict) else str(ch)
                num  = ch.get("idx", ch.get("index", idx)) if isinstance(ch, dict) else idx
                parts.append(f"ch{num}:{name}" if name else f"ch{num}")
            return f"@{sender} channels: {' | '.join(parts)}"
        except asyncio.TimeoutError:
            return f"@{sender} channel fetch timeout"
        except Exception as e:
            log.error("get_channels error: %s", e)
            return f"@{sender} error: {e}"

    async def _cmd_reset_paths(self, args, sender, payload, channel) -> str:
        """Resets routes to all known contacts – switches to flood routing."""
        try:
            contacts_result = await asyncio.wait_for(
                self.mc.commands.get_contacts(), timeout=5.0
            )
            if contacts_result.type == EventType.ERROR:
                return f"@{sender} error fetching contacts: {contacts_result.payload}"
            contacts = contacts_result.payload or {}
            if not contacts:
                return f"@{sender} no contacts – no routes to reset"

            reset_ok   = []
            reset_fail = []
            for key, contact in contacts.items():
                try:
                    r = await asyncio.wait_for(
                        self.mc.commands.reset_path(contact), timeout=3.0
                    )
                    name = contact.get("adv_name", key[:6])
                    if r.type == EventType.ERROR:
                        reset_fail.append(name)
                    else:
                        reset_ok.append(name)
                except Exception:
                    reset_fail.append(contact.get("adv_name", key[:6]))

            ok_str   = f"ok: {', '.join(reset_ok)}" if reset_ok else ""
            fail_str = f" error: {', '.join(reset_fail)}" if reset_fail else ""
            return f"@{sender} path reset {ok_str}{fail_str} | flood routing active"
        except asyncio.TimeoutError:
            return f"@{sender} timeout – reset failed"
        except Exception as e:
            log.error("reset_path error: %s", e)
            return f"@{sender} error: {e}"

    def _cmd_help(self, args, sender, payload, channel) -> str:
        bp = self.cfg.get("bot_prefix", "!bot")
        ap = self.cfg.get("ai_prefix",  "!ai")
        return (
            f"@{sender} {bp}: ping test info stats path snr "
            f"weather <city> news search <what> "
            f"channel channels reset monitor [on/off] | "
            f"{ap}: <question> reset | priv: works with prefix"
        )

    # ── Asynchronous ────────────────────────────────────────────────────────
    async def _cmd_snr(self, args, sender, payload, channel) -> str:
        snr  = _p(payload, "snr")
        hops = _to_int(_p(payload, "path_len", default=0))
        rssi = _p(payload, "rssi")
        q    = _snr_quality(snr)
        prompt = (
            f"LoRa connection parameters: SNR={snr}dB ({q}), "
            f"RSSI={rssi}dBm, hops={hops}. "
            f"Assess the quality and provide a brief recommendation. Max 200 chars. Text only."
        )
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, self.llm.analyze, prompt
        )
        return f"@{sender} {analysis}"

    async def _cmd_search(self, args, sender, payload, channel) -> str:
        if not args.strip():
            return f"@{sender} provide what to search: {self.cfg.get('bot_prefix')} search bitcoin"
        result = await asyncio.get_event_loop().run_in_executor(
            None, self.web.search, args.strip()
        )
        return f"@{sender} {result}"

    def _cmd_monitor(self, args, sender, payload, channel) -> str:
        """Enables/disables passive SNR monitoring on the channel."""
        if channel is None:
            return f"@{sender} monitor only works on group channels."
        cmd = args.strip().lower()
        bp  = self.cfg.get("bot_prefix", "!bot")
        if cmd in ("on", "enable", "start", "1"):
            self._monitored_channels.add(channel)
            return (
                f"@{sender} Monitor ch{channel} enabled. "
                f"I will warn when SNR<0 or connection is critical."
            )
        if cmd in ("off", "disable", "stop", "0"):
            self._monitored_channels.discard(channel)
            return f"@{sender} Monitor ch{channel} disabled."
        # No argument – show status
        status = "enabled" if channel in self._monitored_channels else "disabled"
        return (
            f"@{sender} Monitor ch{channel}: {status}. "
            f"Use: {bp} monitor on / off"
        )

    def get_monitor_report(self) -> list[tuple[int, str]]:
        """
        Returns a list of (channel, report_str) for all monitored channels.
        Called cyclically by _monitor_reminder_loop.
        """
        reports = []
        for ch in self._monitored_channels:
            hist = self._chan_history.get(ch)
            if not hist:
                reports.append((ch, f"📡 Monitor ch{ch}: no messages since last report."))
                continue

            entries = list(hist)
            per_sender: dict[str, list[float]] = {}
            for m in entries:
                try:
                    per_sender.setdefault(m["sender"], []).append(float(m["snr"]))
                except (TypeError, ValueError):
                    pass

            if not per_sender:
                reports.append((ch, f"📡 Monitor ch{ch}: no SNR data."))
                continue

            all_snr = [v for vals in per_sender.values() for v in vals]
            g_avg   = sum(all_snr) / len(all_snr)
            g_min   = min(all_snr)
            g_max   = max(all_snr)

            # Stations with a weak signal
            weak = [
                f"{s} avg:{sum(v)/len(v):.1f}dB"
                for s, v in per_sender.items()
                if sum(v) / len(v) < 0
            ]
            weak_str = " WEAK: " + ", ".join(weak) if weak else ""

            # Overall quality indicator
            icon = "✅" if g_avg >= 5 else ("△" if g_avg >= 0 else "⚠️")
            sender_count = len(per_sender)
            msg_count    = len(entries)

            reports.append((ch, (
                f"📡 Monitor ch{ch} {icon} "
                f"avg:{g_avg:.1f} min:{g_min:.0f} max:{g_max:.0f}dB "
                f"| {sender_count} stations {msg_count} msg{weak_str}"
            )))
        return reports

    def check_monitor(self, channel: int | None, sender: str, payload: dict) -> str | None:
        """
        Called on every message – returns a warning or None.
        Only when monitor is enabled on this channel and SNR is weak.
        """
        if channel is None or channel not in self._monitored_channels:
            return None
        snr = _p(payload, "snr", default=None)
        if snr is None:
            return None
        try:
            snr_f = float(snr)
        except (TypeError, ValueError):
            return None
        q = _snr_quality(snr_f)
        if snr_f < -10:
            return f"⚠️ {sender} SNR:{snr}dB ({q}) – critical connection!"
        if snr_f < 0:
            return f"△ {sender} SNR:{snr}dB ({q})"
        return None  # SNR ok – do not comment

    async def _cmd_chan_analysis(self, args, sender, payload, channel) -> str:
        if channel is None:
            return f"@{sender} channel analysis only works on group channels."
        hist = self._chan_history.get(channel)
        if not hist or len(hist) < 3:
            return f"@{sender} not enough messages in history (min. 3)."

        entries = list(hist)[-20:]

        # ── SNR Statistics per sender ───────────────────────────────────────
        per_sender: dict[str, list[float]] = {}
        for m in entries:
            try:
                per_sender.setdefault(m["sender"], []).append(float(m["snr"]))
            except (TypeError, ValueError):
                pass

        # Format per sender: "flyer1 avg:9.2 min:7 max:11dB(excellent)"
        sender_lines = []
        for s, vals in per_sender.items():
            avg = sum(vals) / len(vals)
            mn  = min(vals)
            mx  = max(vals)
            q   = _snr_quality(avg)
            warn = "⚠️" if avg < 0 else ("△" if avg < 5 else "")
            sender_lines.append(
                f"{s} avg:{avg:.1f} min:{mn:.0f} max:{mx:.0f}dB {warn}({q})"
            )

        # ── Global Statistics ───────────────────────────────────────────────
        all_snr = [v for vals in per_sender.values() for v in vals]
        if all_snr:
            g_avg = sum(all_snr) / len(all_snr)
            g_min = min(all_snr)
            g_max = max(all_snr)
            global_str = f"ch{channel} avg:{g_avg:.1f} min:{g_min:.0f} max:{g_max:.0f}dB"
        else:
            global_str = f"ch{channel}: no SNR data"

        monitor_str = (
            "monitor: on" if channel in self._monitored_channels
            else f"monitor: off ({self.cfg.get('bot_prefix')} monitor on)"
        )

        # Quick response without AI when not enough data
        if len(entries) < 5 or not all_snr:
            lines = " | ".join(sender_lines[:4])
            return f"@{sender} {global_str} | {lines} | {monitor_str}"

        # ── Full AI Analysis ────────────────────────────────────────────────
        prompt = (
            f"You are analyzing the LoRa mesh network, channel {channel}. "
            f"Global SNR: avg:{g_avg:.1f} min:{g_min:.0f} max:{g_max:.0f}dB. "
            f"Per station:\n" + "\n".join(sender_lines) + "\n\n"
            f"SNR>5=excellent, 0-5=ok, 0 to -10=weak, <-10=critical. "
            f"Hops are not a problem. Point out stations with a weak signal. "
            f"Max 200 chars. Text only."
        )
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, self.llm.analyze, prompt
        )
        return f"@{sender} {global_str} | {analysis} | {monitor_str}"


# ─── Bridge ────────────────────────────────────────────────────────────────
class MeshCoreLLMBridge:

    def __init__(self, config: dict):
        self.cfg       = config
        self.llm       = LMStudioClient(
            url           = config["lm_url"],
            model         = config["model"],
            system_prompt = config["system_prompt"],
            history_len   = config["history_len"],
        )
        self.web       = WebSearch(config)
        self.mc: MeshCore | None = None
        self.bot: BotCommands | None = None

        # MeshCore event queue – _poll_loop puts, _process_loop consumes
        self._queue: asyncio.Queue = asyncio.Queue()

        # Set of keys for already processed messages – prevents duplicates
        # when polling and subscription deliver the same event twice
        self._seen_ids: set = set()

        # Node telemetry (noise_floor, battery, uptime etc.)
        # Updated at startup and cyclically by _telemetry_loop
        self._telemetry: dict = {}

        # NOTE – there are three separate "histories" in the code:
        #
        # 1. LMStudioClient._histories[sender]
        #    History of CONVERSATIONS with AI, per caller (callsign).
        #    flyer1 has their own, Waldcor has their own - they do not mix.
        #    Cleared via !ai reset.
        #    Length: history_len messages (default 5).
        #
        # 2. BotCommands._chan_history[channel]
        #    Raw log of MESSAGES FROM THE MESH CHANNEL - all of them, not just AI.
        #    Used by !bot channel (SNR analysis) and AI context.
        #    Clears automatically when exceeding channel_history_len (default 20).
        #
        # 3. Channel context in _handle_llm
        #    Not a separate history - it is a one-time slice of _chan_history
        #    injected into the LLM query so AI "sees" recent messages.
        #    Length: channel_context_msgs (default 5).

    # ── Connection ──────────────────────────────────────────────────────────
    async def connect(self):
        port = self.cfg["serial_port"]
        baud = self.cfg["baud_rate"]
        log.info("Connecting to MeshCore on %s @ %d baud...", port, baud)
        self.mc = await MeshCore.create_serial(port, baud)

        device_info = {}
        try:
            info = await asyncio.wait_for(
                self.mc.commands.send_device_query(), timeout=5.0
            )
            if info.type != EventType.ERROR and info.payload:
                device_info = info.payload
                # Save ALL fields to telemetry - we don't filter
                # because every firmware can return different fields
                self._telemetry.update(device_info)
                log.info("Device: %s", device_info)
                log.info("TELEMETRY from device_query: %s", self._telemetry)
        except asyncio.TimeoutError:
            log.warning("device_query timeout.")
        except Exception as e:
            log.warning("device_query error: %s", e)

        # Additional queries – get_bat and get_node_info (different endpoints, non-blocking)
        await self._refresh_telemetry()
        device_info.update(self._telemetry)
        self.bot = BotCommands(device_info, self.cfg, self.llm, self.web, self._telemetry, self.mc)

    # ── Telemetry ───────────────────────────────────────────────────────────
    async def _refresh_telemetry(self):
        """Polls the node for additional telemetry (get_bat, get_node_info)."""

        for method_name, label in [
            ("get_bat",       "get_bat"),
            ("get_node_info", "get_node_info"),
        ]:
            try:
                method = getattr(self.mc.commands, method_name, None)
                if method is None:
                    log.debug("Method %s unavailable in this library version", method_name)
                    continue
                result = await asyncio.wait_for(method(), timeout=5.0)
                log.info("TELEMETRY %s type=%s payload=%s", label, result.type, result.payload)
                if result.type != EventType.ERROR and result.payload:
                    self._telemetry.update(result.payload)
            except asyncio.TimeoutError:
                log.debug("TELEMETRY %s timeout", label)
            except Exception as e:
                log.debug("TELEMETRY %s error: %s", label, e)

        log.info("TELEMETRY total: %s", self._telemetry)

    async def _telemetry_loop(self):
        interval = self.cfg.get("telemetry_interval_s", 300)
        log.info("Telemetry loop start (every %ds)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._refresh_telemetry()
                if self.bot:
                    self.bot.telemetry = self._telemetry
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("telemetry_loop error: %s", e)

    async def _monitor_reminder_loop(self):
        """Sends an SNR report to monitored channels every monitor_reminder_s."""
        interval = self.cfg.get("monitor_reminder_s", 600)
        log.info("Monitor reminder loop start (every %ds)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.bot or not self.bot._monitored_channels:
                    continue
                reports = self.bot.get_monitor_report()
                for ch, msg in reports:
                    # Send report to the given channel
                    # orig_event is not needed for channel send
                    await self._send_to_channel(msg, ch)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("monitor_reminder_loop error: %s", e)

    async def _send_to_channel(self, text: str, channel: int):
        """Sends a message to the channel without orig_event (for loop reminders)."""
        text = _fit_to_bytes(text, BYTE_LIMIT)
        log.info(">> REMINDER [ch%d] (%dB): %s", channel, len(text.encode()), text)
        try:
            result = await self.mc.commands.send_chan_msg(channel, text)
            if result.type == EventType.ERROR:
                log.error("Error sending reminder ch%d: %s", channel, result.payload)
        except Exception as e:
            log.error("_send_to_channel error: %s", e)

    # ── Deduplication and Queuing ───────────────────────────────────────────
    def _on_event(self, event):
        payload  = event.payload or {}
        txt_hash = payload.get("txt_hash")
        msg_key  = (
            txt_hash
            or f"{payload.get('text','')}|{payload.get('channel_idx', payload.get('channel',''))}"
        )
        if msg_key in self._seen_ids:
            log.debug("DUPLICATE skipped: %s", msg_key)
            return
        self._seen_ids.add(msg_key)
        if len(self._seen_ids) > 500:
            self._seen_ids.clear()
        log.info("QUEUE << type=%s", event.type)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("Queue full")

    # ── Parsing ─────────────────────────────────────────────────────────────
    def _parse_event(self, event) -> tuple[str, str, int | None, dict] | None:
        payload = event.payload or {}
        log.info("RAW EVENT type=%s  payload=%s", event.type, payload)
        if event.type == EventType.CONTACT_MSG_RECV:
            text    = payload.get("text", "").strip()
            sender  = (_p(payload, "adv_name", "name", "pubkey_prefix", default="UNKNOWN"))
            channel = None
        elif event.type == EventType.CHANNEL_MSG_RECV:
            text    = payload.get("text", "").strip()
            sender  = (_p(payload, "adv_name", "name", "pubkey_prefix", default="UNKNOWN"))
            channel = _to_int(_p(payload, "channel_idx", "channel", default=0))
        else:
            return None
        if not text:
            return None
        return sender, text, channel, payload

    @staticmethod
    def _extract_body(text: str) -> tuple[str, str]:
        if ": " in text:
            cs, body = text.split(": ", 1)
            return cs.strip(), body.strip()
        return "", text.strip()

    # ── Processing Loop ─────────────────────────────────────────────────────
    async def _process_loop(self):
        while True:
            event = await self._queue.get()
            try:
                parsed = self._parse_event(event)
                if parsed is None:
                    continue

                sender, text, channel, payload = parsed

                listen = self.cfg.get("listen_channels")
                if channel is not None and listen is not None and channel not in listen:
                    continue

                cs, body = self._extract_body(text)
                if sender == "UNKNOWN" and cs:
                    sender = cs

                log.info("<< [ch%s] %s: %s",
                         channel if channel is not None else "direct", sender, body)

                reply_ch = self.cfg.get("reply_channel")
                if reply_ch is None and channel is not None:
                    reply_ch = channel

                self.bot.record_message(channel, sender, body, payload)

                # ── Passive SNR monitoring ──────────────────────────────────
                monitor_warn = self.bot.check_monitor(channel, sender, payload)
                if monitor_warn:
                    await self._send(monitor_warn, reply_ch, event)

                mention    = f"@{sender} " if sender and sender != "UNKNOWN" else ""
                bot_prefix = self.cfg.get("bot_prefix", "!bot").lower()
                ai_prefix  = self.cfg.get("ai_prefix",  "!ai").lower()
                body_lower = body.lower()

                # ── Bot Command ─────────────────────────────────────────────
                if body_lower.startswith(bot_prefix):
                    after     = body[len(bot_prefix):].strip()
                    cmd, args = self.bot.match(after)
                    if cmd:
                        log.info("BOT CMD: %s args='%s' from %s", cmd, args, sender)
                        response = await self.bot.handle(cmd, args, sender, payload, channel)
                        if response:
                            await self._send_chunked("", response, reply_ch, event)
                    else:
                        await self._send(
                            f"{mention}unknown command. {self.cfg.get('bot_prefix')} help",
                            reply_ch, event
                        )
                    continue

                # ── LLM Trigger ─────────────────────────────────────────────
                if ai_prefix in body_lower:
                    pos      = body_lower.index(ai_prefix)
                    question = body[pos + len(ai_prefix):].strip()
                    log.info("AI TRIGGER | sender=%s question='%s'", sender, question)
                    await self._handle_llm(sender, question, mention, reply_ch, event, channel)

            except Exception as e:
                log.exception("Error in _process_loop: %s", e)

    async def _handle_llm(self, sender, question, mention, reply_ch, orig_event,
                          channel: int | None = None):
        ai_prefix = self.cfg.get("ai_prefix", "!ai")
        q = question.lower()
        try:
            if q in ("reset", "clear", "new"):
                self.llm.clear_history(sender)
                await self._send(f"{mention}history cleared.", reply_ch, orig_event)
                return
            if q in ("help", "pomoc", "?"):
                await self._send(
                    f"{mention}{ai_prefix} <question> | {ai_prefix} reset",
                    reply_ch, orig_event,
                )
                return
            if not question:
                await self._send(f"{mention}type your question after '{ai_prefix}'.", reply_ch, orig_event)
                return

            # Collect channel context - last N messages (excluding bot messages)
            channel_context = None
            ctx_count = self.cfg.get("channel_context_msgs", 5)
            if ctx_count > 0 and channel is not None and self.bot:
                hist = self.bot._chan_history.get(channel)
                if hist:
                    # Take the latest messages, skip those that are bot commands
                    bot_pfx = self.cfg.get("bot_prefix", "!bot").lower()
                    ai_pfx  = self.cfg.get("ai_prefix", "!ai").lower()
                    context_entries = [
                        m for m in list(hist)
                        if not m["text"].lower().startswith(bot_pfx)
                        and ai_pfx not in m["text"].lower()
                    ]
                    if context_entries:
                        channel_context = context_entries[-ctx_count:]
                        log.debug("Channel context: %d messages", len(channel_context))

            log.info("LM Studio << %s (context: %s msg)",
                     question, len(channel_context) if channel_context else 0)
            answer = await asyncio.get_event_loop().run_in_executor(
                None, self.llm.ask, sender, question, channel_context
            )
            log.info("LM Studio >> %s", answer[:120])
            await asyncio.sleep(self.cfg["reply_delay_s"])
            await self._send_chunked(mention, answer, reply_ch, orig_event)
        except Exception as e:
            log.exception("LLM Error")
            await self._send(f"{mention}internal error.", reply_ch, orig_event)

    # ── Sending ─────────────────────────────────────────────────────────────
    async def _send(self, text: str, channel, orig_event):
        text = _fit_to_bytes(text, BYTE_LIMIT)
        if channel is not None:
            # Channel message (broadcast)
            log.info(">> [ch%d] (%dB): %s", channel, len(text.encode()), text)
            result = await self.mc.commands.send_chan_msg(channel, text)
            if result.type == EventType.ERROR:
                log.error("Error sending ch%d: %s", channel, result.payload)
        else:
            # Private message (direct)
            payload = (orig_event.payload or {})
            dst_key_prefix = payload.get("pubkey_prefix")
            dst_name = _p(payload, "adv_name", "name", default=None)

            contact = None

            # 1. Attempt to find by key prefix (asynchronous)
            if dst_key_prefix:
                try:
                    contacts_result = await asyncio.wait_for(
                        self.mc.commands.get_contacts(), timeout=5.0
                    )
                    if contacts_result.type != EventType.ERROR and contacts_result.payload:
                        contacts_dict = contacts_result.payload
                        # MeshCore returns payload as a dictionary { key: contact_info }
                        for key, c in contacts_dict.items():
                            full_pubkey = c.get("pubkey", key)
                            if str(full_pubkey).startswith(dst_key_prefix):
                                contact = c
                                break
                except asyncio.TimeoutError:
                    log.warning("Timeout fetching contacts from MeshCore.")
                except Exception as e:
                    log.error("Error fetching contacts: %s", e)
            
            # 2. If not found by key, fallback to finding by name
            if contact is None and dst_name and dst_name != "UNKNOWN":
                if hasattr(self.mc, "get_contact_by_name"):
                    contact = self.mc.get_contact_by_name(dst_name)

            if contact:
                # Use full name or full key for logging
                display_name = contact.get("adv_name") or contact.get("pubkey", "?")[:8]
                log.info(">> [direct→%s] (%dB): %s", display_name, len(text.encode()), text)
                await self.mc.commands.send_msg(contact, text)
            else:
                log.warning("Contact not found (prefix=%s name=%s) – cannot reply in priv", dst_key_prefix, dst_name)

    async def _send_chunked(self, prefix: str, answer: str, channel, orig_event):
        max_chunks  = self.cfg.get("max_chunks", 5)
        prefix_b    = len(prefix.encode("utf-8"))
        chunk_bytes = BYTE_LIMIT - prefix_b - 8

        chunks, remaining = [], answer.strip()
        while remaining:
            chunk = remaining.encode("utf-8")[:chunk_bytes].decode("utf-8", errors="ignore")
            chunks.append(chunk)
            remaining = remaining[len(chunk):]

        if len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]
            chunks[-1] = chunks[-1].rstrip() + "…"

        if len(chunks) == 1:
            await self._send(prefix + chunks[0], channel, orig_event)
        else:
            for i, chunk in enumerate(chunks, 1):
                await self._send(f"{prefix}({i}/{len(chunks)}) {chunk}", channel, orig_event)
                await asyncio.sleep(1.5)

    # ── Polling ─────────────────────────────────────────────────────────────
    async def _poll_loop(self):
        interval = self.cfg.get("poll_interval_s", 0.5)
        log.info("Polling loop start (every %.1fs)", interval)
        while True:
            try:
                event = await self.mc.commands.get_msg(timeout=interval)
                if event is not None and event.type not in (
                    EventType.NO_MORE_MSGS, EventType.ERROR
                ):
                    self._on_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("get_msg: %s", e)
                await asyncio.sleep(interval)

    # ── Execution ───────────────────────────────────────────────────────────
    async def run(self):
        await self.connect()

        c      = self.cfg
        listen = c.get("listen_channels")
        reply  = c.get("reply_channel")
        dev    = self.bot.device_info if self.bot else {}

        W = 60  # frame width
        def row(label, value, width=W):
            line = f"  {label:<22} {value}"
            return line[:width]

        log.info("╔" + "═" * W + "╗")
        log.info("║  %-*s║", W - 1, " flyer AI bridge — ready!")
        log.info("╠" + "═" * W + "╣")

        # Device
        log.info("║  %-*s║", W - 1, "── Device ──────────────────────────────────")
        log.info("║%s║", row("Device model:", dev.get("model", "?")))
        log.info("║%s║", row("Firmware:", f"{dev.get('ver','?')} ({dev.get('fw_build','?')})"))
        t = self._telemetry
        if t.get("level"):
            pct = t["level"] // 10
            log.info("║%s║", row("Battery:", f"{pct}% (level={t['level']})"))
        if t.get("used_kb"):
            log.info("║%s║", row("RAM Memory:", f"{t.get('used_kb')}kB / {t.get('total_kb','?')}kB"))

        # LLM
        log.info("╠" + "═" * W + "╣")
        log.info("║  %-*s║", W - 1, "── AI Model ────────────────────────────────")
        log.info("║%s║", row("Model:", c["model"]))
        log.info("║%s║", row("URL:", c["lm_url"]))
        log.info("║%s║", row("Conversation history:", f"{c['history_len']} messages per caller"))
        log.info("║%s║", row("Channel context:", f"{c.get('channel_context_msgs',5)} recent msg"))

        # Commands
        log.info("╠" + "═" * W + "╣")
        log.info("║  %-*s║", W - 1, "── Commands ────────────────────────────────")
        log.info("║%s║", row("AI prefix:", f"{c.get('ai_prefix')} <question>  |  {c.get('ai_prefix')} reset"))
        log.info("║%s║", row("Bot prefix:", f"{c.get('bot_prefix')} <command>"))
        log.info("║%s║", row("Available commands:", "ping test info stats path snr"))
        log.info("║%s║", row("", "weather news search channel monitor help"))

        # Network
        log.info("╠" + "═" * W + "╣")
        log.info("║  %-*s║", W - 1, "── Mesh Network ────────────────────────────")
        log.info("║%s║", row("Serial port:", f"{c['serial_port']} @ {c['baud_rate']} baud"))
        log.info("║%s║", row("Listening channels:", "all" if listen is None else str(listen)))
        log.info("║%s║", row("Reply channel:", "same as question" if reply is None else f"ch{reply}"))
        log.info("║%s║", row("Message limit:", f"{BYTE_LIMIT}B (max {c.get('max_chunks')} packets)"))

        # Timers
        log.info("╠" + "═" * W + "╣")
        log.info("║  %-*s║", W - 1, "── Timers ──────────────────────────────────")
        log.info("║%s║", row("MeshCore polling:", f"every {c.get('poll_interval_s', 0.5)}s"))
        log.info("║%s║", row("Telemetry:", f"every {c.get('telemetry_interval_s', 300)}s"))
        log.info("║%s║", row("Monitor reminder:", f"every {c.get('monitor_reminder_s', 600)}s"))
        log.info("║%s║", row("Channel history:", f"{c.get('channel_history_len', 20)} messages"))

        # NewsAPI
        log.info("╠" + "═" * W + "╣")
        log.info("║  %-*s║", W - 1, "── Integrations ────────────────────────────")
        news_status = "✓ configured" if c.get("news_api_key") else "✗ no key (--news-key)"
        log.info("║%s║", row("NewsAPI:", news_status))
        log.info("║%s║", row("Open-Meteo weather:", "✓ no key needed"))
        log.info("║%s║", row("DuckDuckGo search:", "✓ no key needed"))

        log.info("╚" + "═" * W + "╝")
        log.info("Ctrl+C to stop.")

        await asyncio.gather(
            self._process_loop(),
            self._poll_loop(),
            self._telemetry_loop(),
            self._monitor_reminder_loop(),
        )


# ─── CLI ───────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="MeshCore ↔ LM Studio bridge + bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
  python AIbridge.py
  python AIbridge.py --bot-prefix "!f" --ai-prefix "!q"
  python AIbridge.py --news-key abc123
  python AIbridge.py --listen-channels 2 --reply-channel 2
  python AIbridge.py --telemetry-interval 60
        """,
    )
    p.add_argument("--port",               default=DEFAULT_CONFIG["serial_port"])
    p.add_argument("--baud",               type=int, default=DEFAULT_CONFIG["baud_rate"])
    p.add_argument("--model",              default=DEFAULT_CONFIG["model"])
    p.add_argument("--url",                default=DEFAULT_CONFIG["lm_url"])
    p.add_argument("--ai-prefix",          default=DEFAULT_CONFIG["ai_prefix"])
    p.add_argument("--bot-prefix",         default=DEFAULT_CONFIG["bot_prefix"])
    p.add_argument("--news-key",            default=None)
    p.add_argument("--telemetry-interval",  type=int, default=300)
    p.add_argument("--monitor-reminder",    type=int, default=600,
                   help="How often in seconds to send monitored channels report (default 600)")
    p.add_argument("--channel-context",     type=int, default=5,
                   help="How many recent channel messages to add to AI context (0=disabled)")

    ch = p.add_argument_group("Channels")
    ch.add_argument("--listen-channels", nargs="+", type=int, metavar="N")
    ch.add_argument("--reply-channel",   type=int, metavar="N")
    return p.parse_args()


async def main():
    args   = parse_args()
    config = DEFAULT_CONFIG.copy()
    config.update({
        "serial_port":          args.port,
        "baud_rate":            args.baud,
        "model":                args.model,
        "lm_url":               args.url,
        "ai_prefix":            args.ai_prefix,
        "bot_prefix":           args.bot_prefix,
        "news_api_key":          args.news_key,
        "telemetry_interval_s":  args.telemetry_interval,
        "monitor_reminder_s":    args.monitor_reminder,
        "channel_context_msgs":  args.channel_context,
        "listen_channels":      args.listen_channels,
        "reply_channel":        args.reply_channel,
    })

    bridge = MeshCoreLLMBridge(config)
    try:
        await bridge.run()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        if bridge.mc:
            try:
                await bridge.mc.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())