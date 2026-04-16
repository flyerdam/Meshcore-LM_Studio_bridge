"""
Bot commands dispatched via the mesh network (e.g. !bot ping, !bot info …).
"""

import asyncio
import inspect
import logging
from collections import deque
from datetime import datetime

from meshcore import EventType

from meshcore_bridge.helpers import (
    get_payload_value,
    hops_quality,
    snr_quality,
    to_int,
    uptime_str,
)
from meshcore_bridge.llm_client import LMStudioClient
from meshcore_bridge.web_search import WebSearch

log = logging.getLogger(__name__)


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

    # Reverse of CMDS: "_cmd_ping" → "ping" — built once at class-level
    _CMD_KEY: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._CMD_KEY = {v: k for k, v in cls.CMDS.items()}

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
        # Ensure reverse lookup is populated for this class (covers direct instantiation)
        if not BotCommands._CMD_KEY:
            BotCommands._CMD_KEY = {v: k for k, v in self.CMDS.items()}

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
            "snr":    get_payload_value(payload, "snr", default="?"),
            "hops":   get_payload_value(payload, "path_len", default=0),
            "ts":     get_payload_value(payload, "sender_timestamp", default=0),
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
        cmd_key = self._CMD_KEY.get(cmd_name)
        disabled = self.cfg.get("disabled_commands", set())
        if cmd_key and cmd_key in disabled:
            return f"@[{sender}] This feature is not available."
        method = getattr(self, cmd_name, None)
        if method:
            if inspect.iscoroutinefunction(method):
                return await method(args, sender, payload, channel)
            return method(args, sender, payload, channel)
        return ""

    # ── Synchronous ─────────────────────────────────────────────────────────
    def _cmd_ping(self, args, sender, payload, channel) -> str:
        snr  = get_payload_value(payload, "snr")
        hops = to_int(get_payload_value(payload, "path_len", default=0))
        q    = snr_quality(snr)
        return f"@[{sender}] Pong! 🏓 SNR:{snr}({q}) {hops_quality(hops)}"

    def _cmd_test(self, args, sender, payload, channel) -> str:
        snr  = get_payload_value(payload, "snr")
        hops = to_int(get_payload_value(payload, "path_len", default=0))
        ts   = to_int(get_payload_value(payload, "sender_timestamp", default=0))
        t    = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
        q    = snr_quality(snr)
        return f"@[{sender}] Ack! ✅ SNR:{snr}({q}) {hops_quality(hops)} {t}"

    def _cmd_info(self, args, sender, payload, channel) -> str:
        d = self.device_info or {}
        t = self.telemetry   or {}

        mdl = d.get("model", "?")
        ver = d.get("ver", "?")

        upt = t.get("total_up_time_secs") or d.get("total_up_time_secs")
        upt_str = uptime_str(to_int(upt)) if upt else "?"

        nfl     = t.get("noise_floor") or d.get("noise_floor")
        nfl_str = f" NF:{nfl}dBm" if nfl is not None else ""

        batt_str = ""
        level    = t.get("level") or d.get("level")
        batt_mv  = t.get("batt_milli_volts") or d.get("batt_milli_volts")
        if batt_mv:
            pct      = max(0, min(100, int((to_int(batt_mv) - 3000) / 12)))
            batt_str = f" bat:{pct}%"
        elif level is not None:
            pct      = to_int(level) // 10
            batt_str = f" bat:{pct}%"

        used = t.get("used_kb") or d.get("used_kb")
        total= t.get("total_kb") or d.get("total_kb")
        mem_str = f" mem:{used}/{total}kB" if used is not None and total else ""

        snr  = get_payload_value(payload, "snr", default=None) or t.get("last_snr")
        rssi = get_payload_value(payload, "rssi", default=None) or t.get("last_rssi")
        rf   = (f" SNR:{snr}"    if snr  is not None else "") + \
               (f" RSSI:{rssi}"  if rssi is not None else "")

        return f"@[{sender}] {mdl} {ver} up:{upt_str}{nfl_str}{batt_str}{mem_str}{rf}"

    def _cmd_stats(self, args, sender, payload, channel) -> str:
        t     = self.telemetry or {}
        recv  = t.get("n_packets_recv",  "?")
        sent  = t.get("n_packets_sent",  "?")
        flood = t.get("n_sent_flood",    "?")
        direct= t.get("n_sent_direct",   "?")
        errs  = t.get("err_events",      0)
        dups  = to_int(t.get("n_direct_dups", 0)) + to_int(t.get("n_flood_dups", 0))
        air   = t.get("total_air_time_secs")
        air_s = f" air:{uptime_str(to_int(air))}" if air else ""
        if recv == "?" and sent == "?":
            available = {k: v for k, v in t.items()
                        if k not in ("model", "ver", "fw_build", "fw ver")}
            if available:
                return f"@[{sender}] telemetry: {available}"
            return f"@[{sender}] no statistical data (firmware does not provide)"
        return f"@[{sender}] rx:{recv} tx:{sent} flood:{flood} dir:{direct} err:{errs} dup:{dups}{air_s}"

    def _cmd_path(self, args, sender, payload, channel) -> str:
        path = get_payload_value(payload, "path", "route", default="")
        if isinstance(path, list):
            path = ">".join(str(p)[:4] for p in path)
        hops = to_int(get_payload_value(payload, "path_len", default=0))
        snr  = get_payload_value(payload, "snr")
        if not path or path == "?":
            path = hops_quality(hops)
        q = snr_quality(snr)
        if hops > 4:
            warn = " ⚠️ long route"
        elif q in ("very weak", "critical"):
            warn = " ⚠️ weak signal"
        else:
            warn = " ✅"
        return f"@[{sender}] path:{path} SNR:{snr}{warn}"

    def _cmd_weather(self, args, sender, payload, channel) -> str:
        return f"@[{sender}] {self.web.weather(args.strip() or None)}"

    def _cmd_news(self, args, sender, payload, channel) -> str:
        return f"@[{sender}] {self.web.news(args.strip() or None)}"

    async def _cmd_channels(self, args, sender, payload, channel) -> str:
        """Fetches the list of configured channels from the device."""
        try:
            result = await asyncio.wait_for(
                self.mc.commands.get_channels(), timeout=5.0
            )
            if result.type == EventType.ERROR:
                return f"@[{sender}] error fetching channels: {result.payload}"
            channels = result.payload
            if not channels:
                return f"@[{sender}] no configured channels"
            if isinstance(channels, dict):
                items = channels.items()
            else:
                items = enumerate(channels)
            parts = []
            for idx, ch in items:
                name = ch.get("name", "") if isinstance(ch, dict) else str(ch)
                num  = ch.get("idx", ch.get("index", idx)) if isinstance(ch, dict) else idx
                parts.append(f"ch{num}:{name}" if name else f"ch{num}")
            return f"@[{sender}] channels: {' | '.join(parts)}"
        except asyncio.TimeoutError:
            return f"@[{sender}] channel fetch timeout"
        except Exception as e:
            log.error("get_channels error: %s", e)
            return f"@[{sender}] error: {e}"

    async def _cmd_reset_paths(self, args, sender, payload, channel) -> str:
        """Resets routes to all known contacts – switches to flood routing."""
        try:
            contacts_result = await asyncio.wait_for(
                self.mc.commands.get_contacts(), timeout=5.0
            )
            if contacts_result.type == EventType.ERROR:
                return f"@[{sender}] error fetching contacts: {contacts_result.payload}"
            contacts = contacts_result.payload or {}
            if not contacts:
                return f"@[{sender}] no contacts – no routes to reset"

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
            return f"@[{sender}] path reset {ok_str}{fail_str} | flood routing active"
        except asyncio.TimeoutError:
            return f"@[{sender}] timeout – reset failed"
        except Exception as e:
            log.error("reset_path error: %s", e)
            return f"@[{sender}] error: {e}"

    def _cmd_help(self, args, sender, payload, channel) -> str:
        bp = self.cfg.get("bot_prefix", "!bot")
        ap = self.cfg.get("ai_prefix",  "!ai")
        return (
            f"@[{sender}] {bp}: ping test info stats path snr "
            f"weather <city> news search <what> "
            f"channel channels reset monitor [on/off] | "
            f"{ap}: <question> reset | priv: works with prefix"
        )

    # ── Asynchronous ────────────────────────────────────────────────────────
    async def _cmd_snr(self, args, sender, payload, channel) -> str:
        snr  = get_payload_value(payload, "snr")
        hops = to_int(get_payload_value(payload, "path_len", default=0))
        rssi = get_payload_value(payload, "rssi")
        q    = snr_quality(snr)
        prompt = (
            f"LoRa connection parameters: SNR={snr}dB ({q}), "
            f"RSSI={rssi}dBm, hops={hops}. "
            f"Assess the quality and provide a brief recommendation. Max 200 chars. Text only."
        )
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, self.llm.analyze, prompt
        )
        return f"@[{sender}] {analysis}"

    async def _cmd_search(self, args, sender, payload, channel) -> str:
        if not args.strip():
            return f"@[{sender}] provide what to search: {self.cfg.get('bot_prefix')} search bitcoin"
        result = await asyncio.get_event_loop().run_in_executor(
            None, self.web.search, args.strip()
        )
        return f"@[{sender}] {result}"

    def _cmd_monitor(self, args, sender, payload, channel) -> str:
        """Enables/disables passive SNR monitoring on the channel."""
        if channel is None:
            return f"@[{sender}] monitor only works on group channels."
        cmd = args.strip().lower()
        bp  = self.cfg.get("bot_prefix", "!bot")
        if cmd in ("on", "enable", "start", "1"):
            self._monitored_channels.add(channel)
            return (
                f"@[{sender}] Monitor ch{channel} enabled. "
                f"I will warn when SNR<0 or connection is critical."
            )
        if cmd in ("off", "disable", "stop", "0"):
            self._monitored_channels.discard(channel)
            return f"@[{sender}] Monitor ch{channel} disabled."
        status = "enabled" if channel in self._monitored_channels else "disabled"
        return (
            f"@[{sender}] Monitor ch{channel}: {status}. "
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

            weak = [
                f"{s} avg:{sum(v)/len(v):.1f}dB"
                for s, v in per_sender.items()
                if sum(v) / len(v) < 0
            ]
            weak_str = " WEAK: " + ", ".join(weak) if weak else ""

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
        snr = get_payload_value(payload, "snr", default=None)
        if snr is None:
            return None
        try:
            snr_f = float(snr)
        except (TypeError, ValueError):
            return None
        q = snr_quality(snr_f)
        if snr_f < -10:
            return f"⚠️ {sender} SNR:{snr}dB ({q}) – critical connection!"
        if snr_f < 0:
            return f"△ {sender} SNR:{snr}dB ({q})"
        return None  # SNR ok – do not comment

    async def _cmd_chan_analysis(self, args, sender, payload, channel) -> str:
        if channel is None:
            return f"@[{sender}] channel analysis only works on group channels."
        hist = self._chan_history.get(channel)
        if not hist or len(hist) < 3:
            return f"@[{sender}] not enough messages in history (min. 3)."

        entries = list(hist)[-20:]

        per_sender: dict[str, list[float]] = {}
        for m in entries:
            try:
                per_sender.setdefault(m["sender"], []).append(float(m["snr"]))
            except (TypeError, ValueError):
                pass

        sender_lines = []
        for s, vals in per_sender.items():
            avg = sum(vals) / len(vals)
            mn  = min(vals)
            mx  = max(vals)
            q   = snr_quality(avg)
            warn = "⚠️" if avg < 0 else ("△" if avg < 5 else "")
            sender_lines.append(
                f"{s} avg:{avg:.1f} min:{mn:.0f} max:{mx:.0f}dB {warn}({q})"
            )

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

        if len(entries) < 5 or not all_snr:
            lines = " | ".join(sender_lines[:4])
            return f"@[{sender}] {global_str} | {lines} | {monitor_str}"

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
        return f"@[{sender}] {global_str} | {analysis} | {monitor_str}"
