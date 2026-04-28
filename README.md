# MeshCore AI Bridge

A Python companion service that bridges a LoRa mesh network (via [MeshCore](https://github.com/ripplebiz/MeshCore) over USB/Serial) with a local or remote Large Language Model — now with a full graphical interface.

Mesh users can chat with an AI, fetch live internet data (weather, news, web search), and run network diagnostics — all over RF. Long AI responses are automatically chunked to respect LoRa packet limits. The AI sees recent channel context so it can follow ongoing conversations.

---

## Features

- **Modern GUI** (`gui_launcher.py`) — customtkinter dark-mode interface with:
  - Live log console
  - **OSM Map tab** — real-time node map powered by OpenStreetMap tiles; markers color-coded by node type (teal = repeater, mauve = companion); click any node in the list to zoom the map
  - **Nodes tab** — scrollable heard-node list sorted by last seen, with GPS, SNR, age, hover highlight and click-to-zoom
  - Per-provider model memory, feature toggles, message cooldown, restart banner
- **Multi-provider LLM support** — local OpenAI-compatible (LM Studio, Ollama, etc.) or GitHub Models
- **Auto-engage gate** — optional local LLM decides whether a message is worth an AI reply
- **SNR / hop injection** — real signal values from incoming packets injected into AI context (no hallucination)
- **Advert / node tracking** — listens to `ADVERTISEMENT` and `NEW_CONTACT` events; resolves names from contact cache; tracks GPS from `adv_lat`/`adv_lon`
- **Web tools** — weather (Open-Meteo), web search (DuckDuckGo), news headlines (NewsAPI)
- **Network diagnostics** — ping, test, SNR analysis, path info, channel stats, passive monitor

---

## Quick Start

**Prerequisites:** Python 3.10+, LoRa radio on USB/Serial, LLM backend (local or cloud).

```bash
pip install meshcore customtkinter tkintermapview requests pillow
```

**GUI (recommended):**
```bash
python gui_launcher.py
```

**Headless / CLI:**
```bash
python AIbridge.py --port COM3
```

On first run the GUI writes `bridge_config.user.json` with your settings. This file is gitignored — never committed.

---

## Configuration

All settings are available in the GUI. For headless use, copy `bridge_config.default.json` to `bridge_config.user.json` and edit it, or pass CLI flags.

Key CLI flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `COM3` | Serial port |
| `--baud` | `115200` | Baud rate |
| `--provider` | `openai_compat` | `openai_compat` or `github_models` |
| `--model` | — | Model name passed to LLM |
| `--url` | `http://localhost:1234/...` | Chat completions endpoint |
| `--api-key` | — | Bearer token (GitHub Models: PAT with `models:read`) |
| `--news-key` | — | NewsAPI key (newsapi.org, free tier: 100 req/day) |
| `--listen-channels` | all | Restrict to specific channel indices |
| `--ai-prefix` | `!ai` | Trigger prefix for LLM queries |
| `--bot-prefix` | `!b` | Trigger prefix for bot commands |

---

## Mesh Commands

### AI (`!ai`)
| Command | Description |
|---------|-------------|
| `!ai <question>` | Query the LLM (auto-chunked to 130 B packets) |
| `!ai reset` | Clear your conversation history |

### Bot (`!b`)
| Command | Description |
|---------|-------------|
| `!b ping` / `!b test` | Pong with SNR + hop count |
| `!b info` / `!b stats` | Node firmware, battery, packet stats |
| `!b path` / `!b snr` | Signal quality and routing analysis |
| `!b channel` | AI-driven channel analysis |
| `!b weather <city>` | Current weather via Open-Meteo (no key needed) |
| `!b search <query>` | DuckDuckGo instant answer |
| `!b news [topic]` | Top headlines (requires NewsAPI key) |
| `!b monitor on/off` | Toggle passive SNR monitoring |
| `!b reset` | Reset paths to all contacts (flood routing) |

---

## Local files (gitignored)

These files are created at runtime and never committed:

| File | Contents |
|------|----------|
| `bridge_config.user.json` | Your personal settings incl. API keys |
| `bridge_gui_config.json` | Legacy GUI config (migrated automatically) |
| `discoveries.json` | Heard-node cache (callsigns, GPS, SNR) |
| `tile_cache.db` | OSM tile cache for offline/faster map reloads |
| `AIbridge.log` | Runtime log |

---

## Acknowledgments

- **MeshCore** — Python library by [ripplebiz](https://github.com/ripplebiz/MeshCore)
- **tkintermapview** — OSM map widget by TomSchimansky
- **AI assistance** — Parts of this codebase developed with GitHub Copilot (Claude Sonnet)
