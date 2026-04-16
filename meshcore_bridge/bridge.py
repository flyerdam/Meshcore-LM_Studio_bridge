"""
Main bridge: ties MeshCore events to the LLM, bot commands, and web services.
"""

import asyncio
import logging
import time

from meshcore import EventType

from meshcore_bridge.bot_commands import BotCommands
from meshcore_bridge.config import BYTE_LIMIT
from meshcore_bridge.helpers import fit_to_bytes, get_payload_value, to_int
from meshcore_bridge.llm_client import LMStudioClient
from meshcore_bridge.serial_connection import SerialConnection
from meshcore_bridge.web_search import WebSearch

log = logging.getLogger(__name__)


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
        self.serial    = SerialConnection(
            port=config["serial_port"],
            baud=config["baud_rate"],
            cfg=config,
        )
        self.bot: BotCommands | None = None

        # MeshCore event queue – _poll_loop puts, _process_loop consumes
        self._queue: asyncio.Queue = asyncio.Queue()

        # Set of keys for already processed messages – prevents duplicates
        # when polling and subscription deliver the same event twice
        self._seen_ids: set = set()

        # Node telemetry (noise_floor, battery, uptime etc.)
        # Updated at startup and cyclically by _telemetry_loop
        self._telemetry: dict = {}

        # Per-sender timestamp of last reply (for message_cooldown_s)
        self._last_reply: dict[str, float] = {}

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
        log.info("Connecting to MeshCore on %s @ %d baud...",
                 self.cfg["serial_port"], self.cfg["baud_rate"])
        mc = await self.serial.connect()

        device_info = {}
        try:
            info = await asyncio.wait_for(
                mc.commands.send_device_query(), timeout=5.0
            )
            if info.type != EventType.ERROR and info.payload:
                device_info = info.payload
                self._telemetry.update(device_info)
                log.info("Device: %s", device_info)
                log.info("TELEMETRY from device_query: %s", self._telemetry)
        except asyncio.TimeoutError:
            log.warning("device_query timeout.")
        except Exception as e:
            log.warning("device_query error: %s", e)

        await self._refresh_telemetry()
        device_info.update(self._telemetry)
        self.bot = BotCommands(device_info, self.cfg, self.llm, self.web,
                               self._telemetry, mc)

    # ── Telemetry ───────────────────────────────────────────────────────────
    async def _refresh_telemetry(self):
        """Polls the node for additional telemetry (get_bat, get_node_info)."""
        mc = self.serial.mc
        if mc is None:
            return

        for method_name, label in [
            ("get_bat",       "get_bat"),
            ("get_node_info", "get_node_info"),
        ]:
            try:
                method = getattr(mc.commands, method_name, None)
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
                    await self._send_to_channel(msg, ch)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("monitor_reminder_loop error: %s", e)

    async def _send_to_channel(self, text: str, channel: int):
        """Sends a message to the channel without orig_event (for loop reminders)."""
        text = fit_to_bytes(text, BYTE_LIMIT)
        log.info(">> REMINDER [ch%d] (%dB): %s", channel, len(text.encode()), text)
        try:
            mc = await self.serial.ensure_connected()
            result = await mc.commands.send_chan_msg(channel, text)
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
            sender  = (get_payload_value(payload, "adv_name", "name", "pubkey_prefix", default="UNKNOWN"))
            channel = None
        elif event.type == EventType.CHANNEL_MSG_RECV:
            text    = payload.get("text", "").strip()
            sender  = (get_payload_value(payload, "adv_name", "name", "pubkey_prefix", default="UNKNOWN"))
            channel = to_int(get_payload_value(payload, "channel_idx", "channel", default=0))
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

                mention    = f"@[{sender}] " if sender and sender != "UNKNOWN" else ""
                bot_prefix = self.cfg.get("bot_prefix", "!bot").lower()
                ai_prefix  = self.cfg.get("ai_prefix",  "!ai").lower()
                body_lower = body.lower()

                is_command = (
                    body_lower.startswith(bot_prefix)
                    or ai_prefix in body_lower
                )

                # ── Per-sender cooldown ─────────────────────────────────────
                cooldown = float(self.cfg.get("message_cooldown_s", 0))
                if cooldown > 0 and is_command:
                    last = self._last_reply.get(sender, 0.0)
                    elapsed = time.monotonic() - last
                    if elapsed < cooldown:
                        remaining = cooldown - elapsed
                        log.info(
                            "COOLDOWN: %s must wait %.1fs more", sender, remaining
                        )
                        continue

                # ── Bot Command ─────────────────────────────────────────────
                if body_lower.startswith(bot_prefix):
                    after     = body[len(bot_prefix):].strip()
                    cmd, args = self.bot.match(after)
                    if cmd:
                        log.info("BOT CMD: %s args='%s' from %s", cmd, args, sender)
                        response = await self.bot.handle(cmd, args, sender, payload, channel)
                        if response:
                            self._last_reply[sender] = time.monotonic()
                            await self._send_chunked("", response, reply_ch, event)
                    else:
                        self._last_reply[sender] = time.monotonic()
                        await self._send(
                            f"{mention}unknown command. {self.cfg.get('bot_prefix')} help",
                            reply_ch, event
                        )
                    continue

                # ── LLM Trigger ─────────────────────────────────────────────
                if ai_prefix in body_lower:
                    if not self.cfg.get("ai_enabled", True):
                        log.info("AI DISABLED — ignoring query from %s", sender)
                        continue
                    pos      = body_lower.index(ai_prefix)
                    question = body[pos + len(ai_prefix):].strip()
                    log.info("AI TRIGGER | sender=%s question='%s'", sender, question)
                    self._last_reply[sender] = time.monotonic()
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

            # Collect channel context
            channel_context = None
            ctx_count = self.cfg.get("channel_context_msgs", 5)
            if ctx_count > 0 and channel is not None and self.bot:
                hist = self.bot._chan_history.get(channel)
                if hist:
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
        text = fit_to_bytes(text, BYTE_LIMIT)
        try:
            mc = await self.serial.ensure_connected()
        except ConnectionError:
            log.error("Cannot send – serial not connected")
            return

        if channel is not None:
            log.info(">> [ch%d] (%dB): %s", channel, len(text.encode()), text)
            try:
                result = await mc.commands.send_chan_msg(channel, text)
                if result.type == EventType.ERROR:
                    log.error("Error sending ch%d: %s", channel, result.payload)
            except (OSError, ConnectionError) as exc:
                log.warning("Serial error sending to ch%d: %s – reconnecting", channel, exc)
                self.serial._connected = False
                await self.serial.reconnect()
        else:
            payload = (orig_event.payload or {})
            dst_key_prefix = payload.get("pubkey_prefix")
            dst_name = get_payload_value(payload, "adv_name", "name", default=None)

            contact = None

            if dst_key_prefix:
                try:
                    contacts_result = await asyncio.wait_for(
                        mc.commands.get_contacts(), timeout=5.0
                    )
                    if contacts_result.type != EventType.ERROR and contacts_result.payload:
                        contacts_dict = contacts_result.payload
                        for key, c in contacts_dict.items():
                            full_pubkey = c.get("pubkey", key)
                            if str(full_pubkey).startswith(dst_key_prefix):
                                contact = c
                                break
                except asyncio.TimeoutError:
                    log.warning("Timeout fetching contacts from MeshCore.")
                except Exception as e:
                    log.error("Error fetching contacts: %s", e)

            if contact is None and dst_name and dst_name != "UNKNOWN":
                if hasattr(mc, "get_contact_by_name"):
                    contact = mc.get_contact_by_name(dst_name)

            if contact:
                display_name = contact.get("adv_name") or contact.get("pubkey", "?")[:8]
                log.info(">> [direct→%s] (%dB): %s", display_name, len(text.encode()), text)
                try:
                    await mc.commands.send_msg(contact, text)
                except (OSError, ConnectionError) as exc:
                    log.warning("Serial error sending DM to %s: %s – reconnecting",
                                display_name, exc)
                    self.serial._connected = False
                    await self.serial.reconnect()
            else:
                log.warning("Contact not found (prefix=%s name=%s) – cannot reply in priv",
                            dst_key_prefix, dst_name)

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
        consecutive_errors = 0
        max_consecutive_errors = 10

        while True:
            try:
                mc = await self.serial.ensure_connected()
                event = await mc.commands.get_msg(timeout=interval)
                consecutive_errors = 0  # reset on success

                if event is not None and event.type not in (
                    EventType.NO_MORE_MSGS, EventType.ERROR
                ):
                    self._on_event(event)
            except asyncio.CancelledError:
                break
            except (OSError, ConnectionError) as exc:
                consecutive_errors += 1
                log.warning(
                    "Serial error in poll loop (%d/%d): %s",
                    consecutive_errors, max_consecutive_errors, exc,
                )
                if consecutive_errors >= max_consecutive_errors:
                    log.error(
                        "Too many consecutive serial errors – triggering reconnect"
                    )
                    self.serial._connected = False
                    try:
                        mc = await self.serial.reconnect()
                        if mc is not None:
                            # Update bot's reference to the new MeshCore instance
                            if self.bot:
                                self.bot.mc = mc
                            consecutive_errors = 0
                    except Exception as re_exc:
                        log.error("Reconnect failed: %s", re_exc)
                await asyncio.sleep(interval)
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

        # Integrations
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
