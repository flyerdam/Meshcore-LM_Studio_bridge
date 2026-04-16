"""
Default configuration and CLI argument parsing.
"""

import argparse

BYTE_LIMIT = 130  # 141B mesh limit – margin for UTF-8 characters

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
        "3. No markdown, asterisks, or lists. Plain text only but you can use emojis. "
        "4. Write in language that someone is writing to you"
        "5. Be concise and to the point. Like a knight - helpful and direct. "
        "6. If you see the channel context below - you can refer to it."
        "7. If you don't have access to solid data, for example packet path or SNR don't make things up, just say that you don't know and encourage the user to use bot commands"
        "8. If the question doesn't require a long answer make your answer as short as possible - we're running out of air time"
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
    "log_level":            "INFO",
}


def parse_args(argv=None):
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
    p.add_argument(
        "--log-level",
        default=DEFAULT_CONFIG["log_level"],
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    ch = p.add_argument_group("Channels")
    ch.add_argument("--listen-channels", nargs="+", type=int, metavar="N")
    ch.add_argument("--reply-channel",   type=int, metavar="N")
    return p.parse_args(argv)


def build_config(args) -> dict:
    """Merge parsed CLI arguments into the default configuration."""
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
        "log_level":            args.log_level,
    })
    return config
