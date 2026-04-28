"""
Default configuration and CLI argument parsing.
"""

import argparse
import json
from pathlib import Path

BYTE_LIMIT = 130  # 141B mesh limit – margin for UTF-8 characters
DEFAULT_OPENAI_COMPAT_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "bridge_config.default.json"
USER_CONFIG_FILE = PROJECT_ROOT / "bridge_config.user.json"
LEGACY_GUI_CONFIG_FILE = PROJECT_ROOT / "bridge_gui_config.json"

DEFAULT_CONFIG = {
    "serial_port":          "COM3",
    "baud_rate":            115200,
    "llm_provider":         "openai_compat",  # openai_compat | github_models
    "lm_url":               DEFAULT_OPENAI_COMPAT_URL,
    "model":                "openai/gemma-3-12b",
    "llm_api_key":          None,
    "local_gate_enabled":   False,  # use separate local LLM for auto-engage decision
    "local_gate_provider":  "openai_compat",
    "local_gate_url":       DEFAULT_OPENAI_COMPAT_URL,
    "local_gate_model":     "openai/gemma-3-12b",
    "local_gate_api_key":   None,
    "github_api_version":   "2026-03-10",
    "model_caps_cache_file": "model_capabilities_cache.json",
    "model_caps_cache_ttl_s": 86400,
    "token_budget_total":      0,  # 0 = unlimited (session runtime budget)
    "token_budget_prompt":     0,  # 0 = unlimited
    "token_budget_completion": 0,  # 0 = unlimited
    "system_prompt": (
        "You are a cool MeshCore bro skater MLG robot named 'flyer AI' operating in a LoRa mesh network via MeshCore. "
        "You converse with radio operators via the LoRa protocol - messages have a 141-byte limit. "
        "\nRESPONSE RULES: "
        "1. Only the final answer - zero train of thought, zero headers. "
        "2. Max 300 characters - you are in a mesh network, long answers are split into packets. "
        "3. No markdown, asterisks, or lists. Plain text only but you can use emojis. "
        "4. Write in language that someone is writing to you - for example if the question is in English - answer in English, if it's in Polish - answer in Polish. "
        "5. Be concise and to the point. Sound chill, sharp, and technical - helpful and direct. "
        "6. If you see the channel context below - you can refer to it."
        "7. If you don't have access to solid data, for example packet path or SNR don't make things up, just say that you don't know and encourage the user to use the bot commands listed in ACTIVE COMMANDS section below. "
        "8. If the question doesn't require a long answer make your answer as short as possible - we're running out of air time"
        "9. Use caveman style for reasoning and output: short words, direct, zero fluff, fast answer. "
        "10. If a message talks to another person and to you in the same line, still answer the part meant for you. "
        "11. Be a cool skater bro robot assistant, not a formal AI - you can use slang and emojis, but keep it short."
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
    "reconnect_delay_s":    5,     # initial delay before reconnection attempt
    "reconnect_max_delay_s": 60,   # maximum delay between reconnection attempts
    "reconnect_max_retries": 0,    # max retries before giving up (0 = infinite)
    # ── GUI / feature-gate settings ─────────────────────────────────────────
    "message_cooldown_s":      0,     # per-sender cooldown between replies (0 = off)
    "ai_enabled":              True,  # whether !ai LLM queries are accepted
    "disabled_commands":       set(), # set of bot command keys to disable (e.g. {"news", "weather"})
    "reply_unknown_command":   True,  # whether to reply when an unknown bot command is received
    "mention_ai_enabled":      True,  # whether @[own_name] in a message triggers AI reply
    "auto_engage_worth_reply": False, # legacy flag (kept for compat)
    "auto_engage_intensity":   "off",  # off | minimal | keyword | normal | aggressive
}


def _json_sanitize(config: dict) -> dict:
    data = dict(config)
    if isinstance(data.get("disabled_commands"), set):
        data["disabled_commands"] = sorted(data["disabled_commands"])
    return data


def _normalize_loaded(data: dict) -> dict:
    cfg = dict(data)
    if isinstance(cfg.get("disabled_commands"), list):
        cfg["disabled_commands"] = set(str(x) for x in cfg["disabled_commands"])

    # Convert stringly/legacy forms into expected runtime types.
    listen = cfg.get("listen_channels")
    if listen in ("", [], (), {}):
        cfg["listen_channels"] = None
    elif isinstance(listen, str):
        nums = [int(x) for x in listen.split() if x.strip().isdigit()]
        cfg["listen_channels"] = nums if nums else None

    reply = cfg.get("reply_channel")
    if reply in ("", [], (), {}):
        cfg["reply_channel"] = None
    else:
        try:
            cfg["reply_channel"] = int(reply)
        except (TypeError, ValueError):
            cfg["reply_channel"] = None

    if cfg.get("llm_api_key") == "":
        cfg["llm_api_key"] = None
    if cfg.get("local_gate_api_key") in ("", "None", "none", None):
        cfg["local_gate_api_key"] = None
    # migrate old intensity names to new tier names
    intensity_migration = {"cautious": "keyword", "aggressive": "aggressive"}
    old_intensity = cfg.get("auto_engage_intensity", "off")
    if old_intensity not in {"off", "minimal", "keyword", "normal", "aggressive"}:
        cfg["auto_engage_intensity"] = intensity_migration.get(old_intensity, "off")
    if cfg.get("news_api_key") in ("", "None", "none", None):
        cfg["news_api_key"] = None
    return cfg


def _load_legacy_gui_config(path: Path) -> dict:
    """Load old bridge_gui_config.json and map it to runtime config keys."""
    raw = _load_json_file(path)
    if not raw:
        return {}

    mapped: dict = {}
    direct_keys = (
        "serial_port", "baud_rate", "llm_provider", "lm_url", "model",
        "llm_api_key", "local_gate_enabled", "local_gate_provider",
        "local_gate_url", "local_gate_model", "local_gate_api_key",
        "ai_prefix", "bot_prefix", "listen_channels",
        "reply_channel", "message_cooldown_s", "reply_delay_s",
        "channel_context_msgs", "token_budget_total", "token_budget_prompt",
        "token_budget_completion", "telemetry_interval_s", "monitor_reminder_s",
        "auto_engage_intensity", "news_api_key",
    )
    for k in direct_keys:
        if k in raw:
            mapped[k] = raw[k]

    feat_map = {
        "feat___ai__": "ai_enabled",
        "feat___reply_unknown__": "reply_unknown_command",
        "feat___mention_ai__": "mention_ai_enabled",
    }
    for old_k, new_k in feat_map.items():
        if old_k in raw:
            mapped[new_k] = bool(raw[old_k])

    # Legacy per-command feature toggles -> disabled_commands set.
    disabled: set[str] = set()
    for k, v in raw.items():
        if not str(k).startswith("feat_"):
            continue
        cmd = str(k)[5:]
        if cmd in {"__ai__", "__reply_unknown__", "__mention_ai__", "__auto_engage__"}:
            continue
        if not bool(v):
            disabled.add(cmd)
    if disabled:
        mapped["disabled_commands"] = disabled

    # Legacy bool to new intensity mode.
    if "feat___auto_engage__" in raw and "auto_engage_intensity" not in mapped:
        mapped["auto_engage_intensity"] = "normal" if bool(raw["feat___auto_engage__"]) else "off"

    return _normalize_loaded(mapped)


def _load_json_file(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _normalize_loaded(data)
    except Exception:
        pass
    return {}


def ensure_default_config_file() -> None:
    """Create bridge_config.default.json on first run if missing."""
    if DEFAULT_CONFIG_FILE.exists():
        return
    try:
        DEFAULT_CONFIG_FILE.write_text(
            json.dumps(_json_sanitize(DEFAULT_CONFIG), indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_persisted_config() -> dict:
    """Load config in priority: built-in defaults < default file < legacy gui file < user file."""
    ensure_default_config_file()
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(_load_json_file(DEFAULT_CONFIG_FILE))
    cfg.update(_load_legacy_gui_config(LEGACY_GUI_CONFIG_FILE))
    cfg.update(_load_json_file(USER_CONFIG_FILE))  # user.json wins over legacy
    cfg = _normalize_loaded(cfg)
    return cfg


def save_user_config(config: dict) -> None:
    """Persist user overrides to bridge_config.user.json."""
    try:
        USER_CONFIG_FILE.write_text(
            json.dumps(_json_sanitize(config), indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def parse_args():
    persisted = load_persisted_config()
    p = argparse.ArgumentParser(
        description="MeshCore ↔ LLM bridge + bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
  python AIbridge.py
  python AIbridge.py --bot-prefix "!f" --ai-prefix "!q"
  python AIbridge.py --news-key abc123
  python AIbridge.py --listen-channels 2 --reply-channel 2
  python AIbridge.py --telemetry-interval 60
        """,
    )
    p.add_argument("--port",               default=persisted["serial_port"])
    p.add_argument("--baud",               type=int, default=persisted["baud_rate"])
    p.add_argument("--provider",           choices=["openai_compat", "github_models"],
                   default=persisted["llm_provider"])
    p.add_argument("--model",              default=persisted["model"])
    p.add_argument("--url",                default=persisted["lm_url"])
    p.add_argument("--api-key",            default=persisted.get("llm_api_key"))
    p.add_argument("--model-caps-file",    default=persisted["model_caps_cache_file"])
    p.add_argument("--model-caps-ttl",     type=int,
                   default=persisted["model_caps_cache_ttl_s"])
    p.add_argument("--token-budget-total", type=int,
                   default=persisted["token_budget_total"])
    p.add_argument("--token-budget-prompt", type=int,
                   default=persisted["token_budget_prompt"])
    p.add_argument("--token-budget-completion", type=int,
                   default=persisted["token_budget_completion"])
    p.add_argument("--ai-prefix",          default=persisted["ai_prefix"])
    p.add_argument("--bot-prefix",         default=persisted["bot_prefix"])
    p.add_argument("--news-key",            default=persisted.get("news_api_key"))
    p.add_argument("--telemetry-interval",  type=int, default=persisted["telemetry_interval_s"])
    p.add_argument("--monitor-reminder",    type=int, default=persisted["monitor_reminder_s"],
                   help="How often in seconds to send monitored channels report (default 600)")
    p.add_argument("--channel-context",     type=int, default=persisted["channel_context_msgs"],
                   help="How many recent channel messages to add to AI context (0=disabled)")

    ch = p.add_argument_group("Channels")
    ch.add_argument("--listen-channels", nargs="+", type=int, metavar="N")
    ch.add_argument("--reply-channel",   type=int, metavar="N")
    return p.parse_args()


def build_config(args) -> dict:
    """Merge parsed CLI arguments into the default configuration."""
    config = load_persisted_config()
    config.update({
        "serial_port":          args.port,
        "baud_rate":            args.baud,
        "llm_provider":         args.provider,
        "model":                args.model,
        "lm_url":               args.url,
        "llm_api_key":          args.api_key,
        "model_caps_cache_file": args.model_caps_file,
        "model_caps_cache_ttl_s": max(0, int(args.model_caps_ttl)),
        "token_budget_total":   max(0, int(args.token_budget_total)),
        "token_budget_prompt":  max(0, int(args.token_budget_prompt)),
        "token_budget_completion": max(0, int(args.token_budget_completion)),
        "ai_prefix":            args.ai_prefix,
        "bot_prefix":           args.bot_prefix,
        "news_api_key":          args.news_key,
        "telemetry_interval_s":  args.telemetry_interval,
        "monitor_reminder_s":    args.monitor_reminder,
        "channel_context_msgs":  args.channel_context,
        "listen_channels":      args.listen_channels,
        "reply_channel":        args.reply_channel,
    })
    return config
