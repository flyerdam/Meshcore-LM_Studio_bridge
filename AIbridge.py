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
import logging
import sys

from meshcore_bridge.config import parse_args, build_config
from meshcore_bridge.bridge import MeshCoreLLMBridge

log = logging.getLogger(__name__)


def setup_logging(level_name: str):
    level = getattr(logging, (level_name or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("AIbridge.log", encoding="utf-8"),
        ],
        force=True,
    )
    log.info("Log level set to %s", logging.getLevelName(level))


async def main():
    args   = parse_args()
    setup_logging(args.log_level)
    config = build_config(args)

    bridge = MeshCoreLLMBridge(config)
    try:
        await bridge.run()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        try:
            await bridge.serial.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
