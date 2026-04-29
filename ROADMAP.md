# MeshCore AI Bridge — Product Roadmap & Feature Guide

## What This App Is

MeshCore AI Bridge is a **radio mesh network bot** that connects a MeshCore LoRa device (Heltec V3 or compatible) to one or more LLM backends. People on the mesh radio network can talk to the bot, ask the AI questions, and use utility commands — all over a 141-byte LoRa packet protocol.

It is designed for:
- Amateur radio operators wanting AI assistance on the mesh
- Emergency communication networks where a node can answer questions autonomously
- Experimenters who want to bridge modern AI with long-range low-bandwidth radio

---

## Core Architecture

```
[LoRa Mesh Network]
      │
  [MeshCore device — Heltec V3]
      │  serial (USB-C / COM port)
      │
  [meshcore_bridge]
      ├── SerialConnection  (asyncio, event-driven)
      ├── BotCommands       (sync/async command handlers)
      ├── LMStudioClient    (LLM HTTP client, multi-provider)
      ├── WebSearch         (weather, news, DuckDuckGo)
      └── MeshCoreLLMBridge (event loop, routing logic)
      │
  [GUI — gui_launcher.py]
      ├── Settings sidebar  (provider, model, prefixes, features)
      ├── Console tab       (live log output)
      ├── Map tab           (OSM tile map + node list)
      └── Dialogs           (API keys, Channel Manager, Add Provider)
```

---

## Message Byte Budget

**Hard constraint**: MeshCore LoRa packets are 141 bytes max. The bridge reserves a margin for UTF-8 multi-byte characters and sets `BYTE_LIMIT = 130`. All bot command responses are capped at this limit. LLM responses are split into up to `max_chunks` packets (default: 5).

---

## Bot Command Reference

All commands triggered by `{bot_prefix}` (default `!b`):

| Command | Args | Description |
|---|---|---|
| `ping` | — | Connection test — returns SNR and hop count |
| `test` | — | Full connection params — SNR, RSSI, hops, timestamp |
| `info` / `status` | — | Device info: firmware, model, uptime, battery, noise floor |
| `stats` | — | Packet stats: rx/tx/flood/direct/errors/duplicates/air time |
| `path` | — | Routing path and quality assessment |
| `snr` | — | AI-powered signal quality analysis (uses LLM) |
| `weather` | `<city>` | Live weather via Open-Meteo (free, no key needed) |
| `news` | `[topic]` | News headlines via NewsAPI (key required) or DDG fallback |
| `search` | `<query>` | Web search via DuckDuckGo instant answer API |
| `channel` | — | SNR analysis of all recent stations on this channel |
| `channels` | — | List all configured mesh channels on the device |
| `reset` | — | Reset routing paths for all contacts → flood routing |
| `monitor` | `on\|off` | Automatic SNR warning alerts on this channel |
| `help` | — | Show all available commands |

**AI queries** triggered by `{ai_prefix}` (default `!ai`):

| Trigger | Action |
|---|---|
| `!ai <question>` | Ask the LLM anything |
| `!ai reset` | Clear conversation history for this sender |
| `!ai help` | Show AI usage hint |
| `@[YourNodeName]` | Direct mention triggers AI reply |

---

## Current Features

### Implemented ✅

**Hardware & Serial**
- Auto-connect to MeshCore device over serial
- Auto-reconnect with exponential backoff
- Configurable baud rate (default 115200)
- Device info & telemetry polling at startup and periodically

**Message Routing**
- Filter by channel (`listen_channels`: `None` = all, or list like `[0, 2]`)
- Message deduplication (seen-ID set)
- Per-sender reply cooldown (`message_cooldown_s`)
- Reply delay configurable (`reply_delay_s`)

**AI Integration**
- Per-sender conversation history (isolated per callsign)
- Configurable history length
- Channel context injection into AI queries (last N channel messages)
- Multi-provider: LM Studio (local), Ollama, OpenAI-compatible, GitHub Models
- Optional local "gate" LLM for auto-engage decision
- Token budget enforcement (total / prompt / completion)
- Model capabilities cache (GitHub Models catalog)
- Think-tag stripping, tool artifact removal, sanitization pipeline

**Auto-Engage Modes**
- `off` — only explicit `!ai` prefix or `@mention`
- `minimal` — AI keyword present AND local LLM confirms
- `keyword` — static rules: `?`, AI words, bot-address keywords
- `normal` — local LLM decides for every message
- `aggressive` — reply to anything non-trivial

**Bot Commands** (all above + special cases)
- SNR quality thresholds (excellent/good/weak/very weak/critical)
- Smart hops warning (> 4 hops flagged with ⚠️)
- Battery % from milli-volts or level field
- Uptime formatting (h/m/s)
- WMO weather code descriptions
- SNR monitor: automatic periodic alerts on watched channels

**Web Services**
- Weather: Open-Meteo geocoding + forecast (free, no key)
- News: NewsAPI top-headlines + query search (key from env or GUI)
- Search: DuckDuckGo instant answers API

**Node Discovery & Map**
- Advert + NEW_CONTACT events → node list + OSM map markers
- GPS coordinates from `adv_lat/adv_lon` or `lat/lon`
- Node type detection: companion (📱 mauve), repeater (🔁 teal), unknown (white)
- Discoveries persisted to `discoveries.json`
- Tile cache for offline map viewing (`tile_cache.db`)
- Map marker icons: glossy PIL circles per node type color

**GUI**
- Catppuccin Mocha dark theme throughout
- Provider presets: LM Studio, Ollama, GitHub Models, OpenAI + custom
- Per-provider last-used model memory
- Bot feature toggles (enable/disable each command individually)
- API Keys dialog (masked entry, env var hints)
- Channel Manager: fetch all slots from device, filter checkboxes, write-back
- Node list: search/filter (debounced 150ms), capped at 100 rows unfiltered
- Per-node context menu: Remove node
- Console: live log with color-coded levels, auto-scroll, clear button
- Bridge start/stop with status indicator
- Auto-start option

**Security**
- API keys read from env vars (`NEWS_API_KEY`, `GITHUB_TOKEN`, `LLM_API_KEY`, `OPENAI_API_KEY`)
- Config files gitignored
- No credentials stored in version-controlled files

---

## Planned / Roadmap

### Short-term

- [ ] **Node details panel** — click node in list → show full telemetry (battery, firmware, uptime, last SNR, path)
- [ ] **Message history per node** — track messages sent/received per callsign, viewable in GUI
- [ ] **Command rate-limit** — per-command cooldown on top of per-sender cooldown (prevent `!b weather` spam)
- [ ] **Custom system prompt per channel** — different AI personality on different channels
- [ ] **Whisper/direct message support** — reply in private DM instead of broadcast
- [ ] **Node rename / alias** — map callsign → friendly name in GUI
- [ ] **Export node list** — CSV/JSON export of all discovered nodes
- [ ] **Alert on node appear/disappear** — GUI notification or sound when new node heard or goes silent

### Medium-term

- [ ] **Multi-device support** — connect to multiple COM ports simultaneously
- [ ] **APRS-style position beaconing** — periodically announce own GPS position
- [ ] **Scheduled broadcasts** — time-triggered messages (e.g. daily weather summary)
- [ ] **LLM tool calls** — let the AI invoke `!b weather`, `!b search` itself via function calling
- [ ] **Web dashboard** — optional Flask/FastAPI web UI for remote monitoring
- [ ] **Prometheus metrics endpoint** — expose node count, message rate, LLM token usage
- [ ] **Plugin system** — allow custom bot commands via external Python modules

### Long-term / Ideas

- [ ] **Mesh-wide AI context** — share channel context across nodes for distributed AI
- [ ] **Offline LLM auto-detection** — scan local network for running inference servers
- [ ] **Signal propagation map** — plot SNR values geographically over time
- [ ] **Emergency keyword detection** — flag messages with emergency keywords, alert operator

---

## Configuration Reference

Key settings in `bridge_gui_config.json` (auto-managed by GUI):

| Key | Default | Description |
|---|---|---|
| `serial_port` | `COM3` | Device serial port |
| `baud_rate` | `115200` | Serial baud rate |
| `llm_provider` | `openai_compat` | `openai_compat` or `github_models` |
| `lm_url` | localhost:1234 | LLM API endpoint |
| `model` | `openai/gemma-3-12b` | Model ID |
| `ai_prefix` | `!ai` | Trigger for AI queries |
| `bot_prefix` | `!b` | Trigger for bot commands |
| `history_len` | `20` | AI conversation history per sender |
| `max_chunks` | `5` | Max LoRa packets per response |
| `listen_channels` | `null` | Which channels to monitor (null = all) |
| `channel_context_msgs` | `20` | Recent messages injected into AI context |
| `message_cooldown_s` | `0` | Per-sender cooldown (0 = off) |
| `auto_engage_intensity` | `off` | Auto-reply mode |
| `token_budget_total` | `0` | Session token cap (0 = unlimited) |
| `news_api_key` | `null` | NewsAPI key (prefer env var) |
| `monitor_reminder_s` | `600` | SNR monitor alert interval |

---

## Limits & Known Constraints

| Constraint | Value | Notes |
|---|---|---|
| LoRa packet MTU | 141 bytes | Hard limit imposed by MeshCore |
| Bridge byte limit | 130 bytes | With UTF-8 margin |
| Max response chunks | 5 (configurable) | = up to 650 bytes split across packets |
| Node list unfiltered cap | 100 rows | Older nodes visible via search |
| Channel history | 50 messages (configurable) | Per channel ring buffer |
| AI history | 20 messages (configurable) | Per sender conversation window |
| Supported device | Heltec V3 (tested) | Other MeshCore devices may work |
| Token budget | 0 = unlimited | Set > 0 to cap API costs |

---

## Running

```bash
# GUI (recommended)
python gui_launcher.py

# CLI (headless / server mode)
python AIbridge.py --port COM3 --model gemma-3-12b

# Run tests
pytest tests/ -v
```

## Dependencies

```
customtkinter      # GUI
tkintermapview     # OSM map widget (optional)
Pillow             # Map marker icons (optional)
meshcore           # MeshCore serial protocol
requests           # Weather / news / search HTTP
pyserial           # Serial port listing
```
