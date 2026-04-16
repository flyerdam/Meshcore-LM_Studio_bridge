#!/usr/bin/env python3
"""
MeshCore-LM Studio Bridge — GUI Launcher  (customtkinter edition)
==================================================================
Run this file instead of AIbridge.py to get a graphical interface.

    python gui_launcher.py

Extra dependency (install once):
    pip install customtkinter
"""

import asyncio
import datetime
import json
import logging
import math
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from types import SimpleNamespace

# ── customtkinter ─────────────────────────────────────────────────────────────
try:
    import customtkinter as ctk
except ImportError:
    sys.exit(
        "customtkinter is not installed.  Run:\n"
        "    pip install customtkinter\n"
        "and try again."
    )

# Optional: list serial ports (pyserial is already a meshcore dependency)
try:
    from serial.tools import list_ports as _serial_list_ports

    def _get_serial_ports() -> list[str]:
        return [p.device for p in _serial_list_ports.comports()]
except ImportError:
    def _get_serial_ports() -> list[str]:
        return []

from meshcore_bridge.config import DEFAULT_CONFIG
from meshcore_bridge.bridge import MeshCoreLLMBridge

log = logging.getLogger(__name__)

# ── Appearance ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Catppuccin Mocha accent palette
P = SimpleNamespace(
    base     = "#1e1e2e",
    mantle   = "#181825",
    crust    = "#11111b",
    surface0 = "#313244",
    surface1 = "#45475a",
    surface2 = "#585b70",
    overlay0 = "#6c7086",
    overlay1 = "#7f849c",
    subtext  = "#a6adc8",
    text     = "#cdd6f4",
    blue     = "#89b4fa",
    sky      = "#89dceb",
    teal     = "#94e2d5",
    green    = "#a6e3a1",
    yellow   = "#f9e2af",
    peach    = "#fab387",
    red      = "#f38ba8",
    mauve    = "#cba6f7",
)

# ── Bot features ──────────────────────────────────────────────────────────────
BOT_FEATURES: list[tuple[str, str]] = [
    ("ping",     "Ping"),
    ("test",     "Test"),
    ("info",     "Info / Status"),
    ("stats",    "Statistics"),
    ("path",     "Path Analysis"),
    ("snr",      "SNR Analysis"),
    ("weather",  "Weather"),
    ("news",     "News Headlines"),
    ("search",   "Web Search"),
    ("channel",  "Channel Analysis"),
    ("channels", "Channel List"),
    ("monitor",  "SNR Monitor"),
    ("reset",    "Reset Paths"),
]

# Persistence & constants
_CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "bridge_gui_config.json")
_STOP_TIMEOUT = 10   # seconds to wait for clean shutdown


# ── Log queue handler ─────────────────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    """Forwards log records into a thread-safe queue for the GUI thread."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord):
        try:
            self._q.put_nowait(record)
        except queue.Full:
            pass


# ── Bridge runner thread ──────────────────────────────────────────────────────

class _BridgeRunner(threading.Thread):
    """
    Runs MeshCoreLLMBridge in a background thread with its own asyncio loop.

    KEY FIX: stop() cancels the root asyncio Task instead of calling
    loop.stop().  Cancellation propagates through asyncio.gather → every
    inner loop → CancelledError is caught in _async_main's finally block →
    bridge.serial.disconnect() is called → COM port is released cleanly.
    """

    def __init__(self, config: dict):
        super().__init__(daemon=True, name="bridge-thread")
        self.config = config
        self.error: Exception | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopped = threading.Event()

    # ── Thread body ───────────────────────────────────────────────────────────

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            # Use create_task so we can cancel the task (not the loop) from stop()
            self._task = self._loop.create_task(self._async_main())
            self._loop.run_until_complete(self._task)
        except (asyncio.CancelledError, RuntimeError):
            pass
        except Exception as exc:
            self.error = exc
            log.error("Bridge crashed: %s", exc)
        finally:
            # Drain any remaining callbacks/tasks before closing
            try:
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass
            self._stopped.set()

    async def _async_main(self):
        bridge = MeshCoreLLMBridge(self.config)
        try:
            await bridge.run()
        except asyncio.CancelledError:
            pass
        finally:
            # This finally block is guaranteed to run even after task.cancel()
            try:
                await asyncio.wait_for(bridge.serial.disconnect(), timeout=5.0)
            except Exception:
                pass

    # ── Control ───────────────────────────────────────────────────────────────

    def stop(self):
        """
        Cancel the root task so all finally-blocks run (COM port closed),
        then wait for the thread to finish.
        """
        if self._loop and not self._loop.is_closed() and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self._stopped.wait(timeout=_STOP_TIMEOUT)

    @property
    def is_running(self) -> bool:
        return self.is_alive() and not self._stopped.is_set()


# ── Custom spinbox widget (CTk-native) ────────────────────────────────────────

class _CTkSpinbox(ctk.CTkFrame):
    """Simple +/- spinbox built from CTkFrame + CTkEntry + two CTkButtons."""

    def __init__(self, parent, from_: float = 0, to: float = 100,
                 increment: float = 1, variable=None, width: int = 160,
                 fmt: str = "{:.0f}", **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._from      = from_
        self._to        = to
        self._increment = increment
        self._fmt       = fmt
        self._var       = variable if variable is not None else ctk.DoubleVar(value=from_)

        btn = dict(width=28, height=28, font=ctk.CTkFont(size=14),
                   fg_color=P.surface1, hover_color=P.surface2,
                   text_color=P.text, corner_radius=6)
        ctk.CTkButton(self, text="\u2212", command=self._dec, **btn).pack(side="left")
        ctk.CTkEntry(self, textvariable=self._var,
                     width=width - 64, justify="center",
                     height=28, corner_radius=6,
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).pack(side="left", padx=3)
        ctk.CTkButton(self, text="+", command=self._inc, **btn).pack(side="left")

    def _inc(self):
        try:
            v = float(str(self._var.get()))
        except (ValueError, TypeError):
            v = self._from
        self._var.set(self._fmt.format(min(self._to, v + self._increment)))

    def _dec(self):
        try:
            v = float(str(self._var.get()))
        except (ValueError, TypeError):
            v = self._from
        self._var.set(self._fmt.format(max(self._from, v - self._increment)))


# ── Main application ──────────────────────────────────────────────────────────

class App(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("MeshCore AI Bridge")
        self.geometry("1240x800")
        self.minsize(900, 600)

        self._runner: _BridgeRunner | None = None
        self._log_queue: queue.Queue       = queue.Queue()

        self._vars:      dict[str, tk.Variable] = {}
        self._feat_vars: dict[str, ctk.BooleanVar] = {}

        # Animation state
        self._pulse_phase   = 0.0
        self._pulse_job: str | None = None

        self._build_ui()
        self._setup_logging()
        self._load_settings()
        self._poll_log()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        self._build_topbar()
        self._build_settings_panel()
        self._build_console_panel()
        self._build_bottombar()

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _build_topbar(self):
        top = ctk.CTkFrame(self, fg_color=P.mantle, corner_radius=0, height=54)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_propagate(False)
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="🛰  MeshCore AI Bridge",
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=P.text).grid(row=0, column=0,
                                             padx=16, pady=14, sticky="w")

        status_frame = ctk.CTkFrame(top, fg_color="transparent")
        status_frame.grid(row=0, column=2, padx=12, pady=14, sticky="e")

        # Animated canvas dot
        self._dot_canvas = tk.Canvas(status_frame, width=18, height=18,
                                     bg=P.mantle, highlightthickness=0)
        self._dot_canvas.pack(side="left", padx=(0, 6))
        self._dot_oval = self._dot_canvas.create_oval(
            3, 3, 15, 15, fill=P.overlay0, outline="")

        self._status_label = ctk.CTkLabel(
            status_frame, text="Stopped",
            font=ctk.CTkFont(size=12), text_color=P.overlay1)
        self._status_label.pack(side="left")

    # ── Settings sidebar ──────────────────────────────────────────────────────

    def _build_settings_panel(self):
        sidebar = ctk.CTkFrame(self, fg_color=P.base, corner_radius=0, width=430)
        sidebar.grid(row=1, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(0, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        self._tab = ctk.CTkTabview(
            sidebar,
            fg_color=P.base,
            segmented_button_fg_color=P.surface0,
            segmented_button_selected_color=P.blue,
            segmented_button_selected_hover_color=P.sky,
            segmented_button_unselected_color=P.surface0,
            segmented_button_unselected_hover_color=P.surface1,
            text_color=P.text,
            text_color_disabled=P.overlay0,
        )
        self._tab.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        for name in ("Connection", "Commands", "Timing", "Features"):
            self._tab.add(name)

        self._build_tab_connection(self._tab.tab("Connection"))
        self._build_tab_commands(self._tab.tab("Commands"))
        self._build_tab_timing(self._tab.tab("Timing"))
        self._build_tab_features(self._tab.tab("Features"))

    # ── shared helpers ────────────────────────────────────────────────────────

    def _sec(self, parent, icon: str, text: str):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=(14, 4))
        ctk.CTkLabel(f, text=f"{icon}  {text}",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=P.blue).pack(side="left", padx=4)
        ctk.CTkFrame(f, height=1, fg_color=P.surface1).pack(
            side="left", fill="x", expand=True, padx=(6, 4))

    def _row(self, parent, label: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=3)
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text=label, text_color=P.subtext,
                     font=ctk.CTkFont(size=12),
                     width=175, anchor="w").grid(row=0, column=0, sticky="w")
        return row

    def _hint(self, parent, text: str):
        ctk.CTkLabel(parent, text=f"   {text}",
                     text_color=P.overlay0,
                     font=ctk.CTkFont(size=10)).pack(anchor="w", padx=8)

    # ── Tab: Connection ───────────────────────────────────────────────────────

    def _build_tab_connection(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        self._sec(scroll, "⚡", "Serial")

        self._vars["serial_port"] = ctk.StringVar(value=DEFAULT_CONFIG["serial_port"])
        ports = _get_serial_ports() or [DEFAULT_CONFIG["serial_port"]]

        row = self._row(scroll, "Port")
        port_inner = ctk.CTkFrame(row, fg_color="transparent")
        port_inner.grid(row=0, column=1, sticky="ew")
        self._port_cb = ctk.CTkComboBox(
            port_inner, values=ports,
            variable=self._vars["serial_port"],
            fg_color=P.surface0, button_color=P.surface1,
            button_hover_color=P.surface2, dropdown_fg_color=P.surface0,
            text_color=P.text, border_width=0, width=190)
        self._port_cb.pack(side="left")
        ctk.CTkButton(port_inner, text="↻", width=32, height=28,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, corner_radius=6,
                      command=self._refresh_ports).pack(side="left", padx=(4, 0))

        self._vars["baud_rate"] = ctk.IntVar(value=DEFAULT_CONFIG["baud_rate"])
        row = self._row(scroll, "Baud Rate")
        _CTkSpinbox(row, from_=1200, to=921600, increment=9600,
                    variable=self._vars["baud_rate"], width=160).grid(
            row=0, column=1, sticky="w")

        self._sec(scroll, "🤖", "LM Studio")

        self._vars["lm_url"] = ctk.StringVar(value=DEFAULT_CONFIG["lm_url"])
        row = self._row(scroll, "API URL")
        ctk.CTkEntry(row, textvariable=self._vars["lm_url"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")

        self._vars["model"] = ctk.StringVar(value=DEFAULT_CONFIG["model"])
        row = self._row(scroll, "Model Name")
        ctk.CTkEntry(row, textvariable=self._vars["model"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")

        self._sec(scroll, "🔗", "News API (optional)")

        self._vars["news_api_key"] = ctk.StringVar(value="")
        row = self._row(scroll, "API Key")
        ctk.CTkEntry(row, textvariable=self._vars["news_api_key"],
                     show="\u25cf",
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")
        self._hint(scroll, "newsapi.org key  (leave blank for DuckDuckGo only)")

    # ── Tab: Commands ─────────────────────────────────────────────────────────

    def _build_tab_commands(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        self._sec(scroll, "💬", "Prefixes")

        self._vars["ai_prefix"]  = ctk.StringVar(value=DEFAULT_CONFIG["ai_prefix"])
        self._vars["bot_prefix"] = ctk.StringVar(value=DEFAULT_CONFIG["bot_prefix"])

        row = self._row(scroll, "AI Prefix")
        ctk.CTkEntry(row, textvariable=self._vars["ai_prefix"],
                     width=100, fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "trigger for LLM queries  (e.g. !ai <question>)")

        row = self._row(scroll, "Bot Prefix")
        ctk.CTkEntry(row, textvariable=self._vars["bot_prefix"],
                     width=100, fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "trigger for bot commands  (e.g. !b ping)")

        self._sec(scroll, "📡", "Channels")

        self._vars["listen_channels"] = ctk.StringVar(value="")
        row = self._row(scroll, "Listen Channels")
        ctk.CTkEntry(row, textvariable=self._vars["listen_channels"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")
        self._hint(scroll, "blank = all  |  space-separated numbers: 0 2 3")

        self._vars["reply_channel"] = ctk.StringVar(value="")
        row = self._row(scroll, "Reply Channel")
        ctk.CTkEntry(row, textvariable=self._vars["reply_channel"],
                     width=80, fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "blank = same channel as the question")

    # ── Tab: Timing ───────────────────────────────────────────────────────────

    def _build_tab_timing(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        self._sec(scroll, "🛡", "Rate Limiting")

        self._vars["message_cooldown_s"] = ctk.DoubleVar(value=0.0)
        row = self._row(scroll, "Message Cooldown (s)")
        _CTkSpinbox(row, from_=0, to=300, increment=1, fmt="{:.0f}",
                    variable=self._vars["message_cooldown_s"],
                    width=160).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "seconds a user must wait between commands  (0 = off)")

        self._sec(scroll, "📨", "Response")

        self._vars["reply_delay_s"] = ctk.DoubleVar(value=DEFAULT_CONFIG["reply_delay_s"])
        row = self._row(scroll, "Reply Delay (s)")
        _CTkSpinbox(row, from_=0.0, to=10.0, increment=0.1, fmt="{:.1f}",
                    variable=self._vars["reply_delay_s"], width=160).grid(
            row=0, column=1, sticky="w")

        self._vars["channel_context_msgs"] = ctk.IntVar(
            value=DEFAULT_CONFIG["channel_context_msgs"])
        row = self._row(scroll, "Channel Context (msgs)")
        _CTkSpinbox(row, from_=0, to=50, increment=1,
                    variable=self._vars["channel_context_msgs"],
                    width=160).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "recent messages injected into AI context  (0 = off)")

        self._sec(scroll, "⏰", "Background Tasks")

        self._vars["telemetry_interval_s"] = ctk.IntVar(
            value=DEFAULT_CONFIG["telemetry_interval_s"])
        row = self._row(scroll, "Telemetry Interval (s)")
        _CTkSpinbox(row, from_=30, to=3600, increment=30,
                    variable=self._vars["telemetry_interval_s"],
                    width=160).grid(row=0, column=1, sticky="w")

        self._vars["monitor_reminder_s"] = ctk.IntVar(
            value=DEFAULT_CONFIG["monitor_reminder_s"])
        row = self._row(scroll, "Monitor Reminder (s)")
        _CTkSpinbox(row, from_=60, to=7200, increment=60,
                    variable=self._vars["monitor_reminder_s"],
                    width=160).grid(row=0, column=1, sticky="w")

    # ── Tab: Features ─────────────────────────────────────────────────────────

    def _build_tab_features(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        self._sec(scroll, "🤖", "AI")

        self._feat_vars["__ai__"] = ctk.BooleanVar(value=True)
        ai_card = ctk.CTkFrame(scroll, fg_color=P.surface0, corner_radius=8)
        ai_card.pack(fill="x", padx=4, pady=4)
        ctk.CTkCheckBox(
            ai_card,
            text="AI Queries  (responds to the !ai prefix)",
            variable=self._feat_vars["__ai__"],
            text_color=P.text,
            checkmark_color=P.base,
            fg_color=P.blue, hover_color=P.sky,
            border_color=P.surface2,
        ).pack(anchor="w", padx=12, pady=10)

        self._sec(scroll, "🧩", "Bot Commands")

        grid = ctk.CTkFrame(scroll, fg_color="transparent")
        grid.pack(fill="x", padx=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        for i, (key, label) in enumerate(BOT_FEATURES):
            self._feat_vars[key] = ctk.BooleanVar(value=True)
            cell = ctk.CTkFrame(grid, fg_color=P.surface0, corner_radius=8)
            cell.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            ctk.CTkCheckBox(
                cell, text=label,
                variable=self._feat_vars[key],
                text_color=P.text,
                checkmark_color=P.base,
                fg_color=P.blue, hover_color=P.sky,
                border_color=P.surface2,
            ).pack(anchor="w", padx=10, pady=8)

    # ── Console panel ─────────────────────────────────────────────────────────

    def _build_console_panel(self):
        right = ctk.CTkFrame(self, fg_color=P.crust, corner_radius=0)
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(right, fg_color=P.mantle, corner_radius=0, height=38)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Console",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=P.blue).grid(row=0, column=0, padx=12,
                                              pady=8, sticky="w")
        ctk.CTkButton(hdr, text="Clear", width=60, height=24,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.subtext, corner_radius=6,
                      font=ctk.CTkFont(size=11),
                      command=self._clear_console).grid(row=0, column=1,
                                                         padx=8, pady=6,
                                                         sticky="e")

        if sys.platform == "win32":
            mono = ("Cascadia Code", 10)
        elif sys.platform == "darwin":
            mono = ("Menlo", 10)
        else:
            mono = ("DejaVu Sans Mono", 10)

        self._console = ctk.CTkTextbox(
            right,
            fg_color=P.crust, text_color=P.text,
            font=mono, wrap="word", state="disabled",
            scrollbar_button_color=P.surface1,
            scrollbar_button_hover_color=P.surface2,
        )
        self._console.grid(row=1, column=0, sticky="nsew")

        tb = self._console._textbox
        tb.tag_configure("TS",       foreground=P.overlay0)
        tb.tag_configure("INFO",     foreground=P.text)
        tb.tag_configure("DEBUG",    foreground=P.overlay1)
        tb.tag_configure("WARNING",  foreground=P.yellow)
        tb.tag_configure("ERROR",    foreground=P.red)
        tb.tag_configure("CRITICAL", foreground=P.red, underline=True)
        tb.tag_configure("RECV",     foreground=P.sky)
        tb.tag_configure("SEND",     foreground=P.green)
        tb.tag_configure("LLM",      foreground=P.mauve)
        tb.tag_configure("BOT",      foreground=P.peach)
        tb.tag_configure("CONN",     foreground=P.teal)

    def _clear_console(self):
        self._console.configure(state="normal")
        self._console.delete("0.0", "end")
        self._console.configure(state="disabled")

    # ── Bottom bar ────────────────────────────────────────────────────────────

    def _build_bottombar(self):
        bot = ctk.CTkFrame(self, fg_color=P.mantle, corner_radius=0, height=50)
        bot.grid(row=2, column=0, columnspan=2, sticky="ew")
        bot.grid_propagate(False)
        bot.grid_columnconfigure(2, weight=1)

        self._start_btn = ctk.CTkButton(
            bot, text="\u25b6  Start",
            fg_color=P.green, hover_color="#b5ebaa",
            text_color=P.base,
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8, width=120, height=34,
            command=self._on_start,
        )
        self._start_btn.grid(row=0, column=0, padx=(12, 4), pady=8)

        self._stop_btn = ctk.CTkButton(
            bot, text="\u25a0  Stop",
            fg_color=P.red, hover_color="#f7829a",
            text_color=P.base,
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8, width=120, height=34,
            state="disabled",
            command=self._on_stop,
        )
        self._stop_btn.grid(row=0, column=1, padx=4, pady=8)

        self._bottom_msg = ctk.CTkLabel(
            bot,
            text="Configure settings and press  Start.",
            text_color=P.subtext,
            font=ctk.CTkFont(size=11),
        )
        self._bottom_msg.grid(row=0, column=2, padx=14, sticky="w")

    # ── Port refresh ──────────────────────────────────────────────────────────

    def _refresh_ports(self):
        ports = _get_serial_ports()
        if ports:
            self._port_cb.configure(values=ports)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _setup_logging(self):
        handler = _QueueHandler(self._log_queue)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        if not any(isinstance(h, logging.StreamHandler) and
                   not isinstance(h, logging.FileHandler)
                   for h in root.handlers):
            root.addHandler(logging.StreamHandler(sys.stdout))

    def _poll_log(self):
        try:
            while True:
                record = self._log_queue.get_nowait()
                self._append_record(record)
        except queue.Empty:
            pass
        self.after(80, self._poll_log)

    def _append_record(self, record: logging.LogRecord):
        ts  = datetime.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg = record.getMessage()
        lc  = msg.lower()

        tag = record.levelname
        if "<<" in msg and ("ch" in lc or "direct" in lc):
            tag = "RECV"
        elif ">>" in msg:
            tag = "SEND"
        elif "lm studio" in lc or "llm" in lc or "ai trigger" in lc:
            tag = "LLM"
        elif "bot cmd" in lc:
            tag = "BOT"
        elif any(kw in lc for kw in ("serial", "connect", "reconnect",
                                      "disconnect", "port")):
            tag = "CONN"

        tb = self._console._textbox
        self._console.configure(state="normal")
        at_end = tb.yview()[1] >= 0.97
        tb.insert("end", f"[{ts}] ", "TS")
        tb.insert("end", f"{msg}\n", tag)
        if at_end:
            tb.see("end")
        self._console.configure(state="disabled")

    # ── Config collect / save / load ──────────────────────────────────────────

    def _collect_config(self) -> dict:
        def _get(key, fallback=None):
            var = self._vars.get(key)
            if var is None:
                return fallback
            try:
                return var.get()
            except Exception:
                return fallback

        config = DEFAULT_CONFIG.copy()

        config["serial_port"]          = _get("serial_port")  or DEFAULT_CONFIG["serial_port"]
        config["baud_rate"]            = int(_get("baud_rate", DEFAULT_CONFIG["baud_rate"]))
        config["lm_url"]               = _get("lm_url")       or DEFAULT_CONFIG["lm_url"]
        config["model"]                = _get("model")        or DEFAULT_CONFIG["model"]
        config["ai_prefix"]            = _get("ai_prefix")    or DEFAULT_CONFIG["ai_prefix"]
        config["bot_prefix"]           = _get("bot_prefix")   or DEFAULT_CONFIG["bot_prefix"]
        config["message_cooldown_s"]   = float(_get("message_cooldown_s", 0))
        config["reply_delay_s"]        = float(_get("reply_delay_s",
                                                    DEFAULT_CONFIG["reply_delay_s"]))
        config["telemetry_interval_s"] = int(float(_get(
            "telemetry_interval_s", DEFAULT_CONFIG["telemetry_interval_s"])))
        config["monitor_reminder_s"]   = int(float(_get(
            "monitor_reminder_s", DEFAULT_CONFIG["monitor_reminder_s"])))
        config["channel_context_msgs"] = int(float(_get(
            "channel_context_msgs", DEFAULT_CONFIG["channel_context_msgs"])))
        config["news_api_key"]         = _get("news_api_key") or None

        listen_raw = (_get("listen_channels") or "").strip()
        config["listen_channels"] = (
            [int(x) for x in listen_raw.split() if x.isdigit()]
            if listen_raw else None
        )
        reply_raw = (_get("reply_channel") or "").strip()
        try:
            config["reply_channel"] = int(reply_raw) if reply_raw else None
        except ValueError:
            config["reply_channel"] = None

        config["ai_enabled"] = self._feat_vars.get(
            "__ai__", ctk.BooleanVar(value=True)).get()
        disabled: set[str] = set()
        for key, _ in BOT_FEATURES:
            if not self._feat_vars.get(key, ctk.BooleanVar(value=True)).get():
                disabled.add(key)
        config["disabled_commands"] = disabled

        return config

    def _save_settings(self):
        data: dict = {}
        for key, var in self._vars.items():
            try:
                data[key] = var.get()
            except Exception:
                pass
        for key, var in self._feat_vars.items():
            try:
                data[f"feat_{key}"] = bool(var.get())
            except Exception:
                pass
        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            log.debug("Could not save settings: %s", exc)

    def _load_settings(self):
        if not os.path.exists(_CONFIG_FILE):
            return
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            log.debug("Could not load settings: %s", exc)
            return
        for key, var in self._vars.items():
            if key in data:
                try:
                    var.set(data[key])
                except Exception:
                    pass
        for key, var in self._feat_vars.items():
            saved = f"feat_{key}"
            if saved in data:
                try:
                    var.set(bool(data[saved]))
                except Exception:
                    pass

    # ── Status & animation ────────────────────────────────────────────────────

    def _set_running(self, running: bool, stopping: bool = False):
        if stopping:
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="disabled")
            self._status_label.configure(text="Stopping\u2026", text_color=P.yellow)
            self._bottom_msg.configure(
                text="Waiting for serial port to close\u2026")
            self._stop_pulse()
            self._dot_canvas.itemconfigure(self._dot_oval, fill=P.yellow)
        elif running:
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="normal")
            self._status_label.configure(text="Running", text_color=P.green)
            self._bottom_msg.configure(
                text="Bridge is running \u2014 watch the console for activity.")
            self._start_pulse()
        else:
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")
            self._status_label.configure(text="Stopped", text_color=P.overlay1)
            self._bottom_msg.configure(
                text="Bridge stopped.  Press  Start  to launch again.")
            self._stop_pulse()
            self._dot_canvas.itemconfigure(self._dot_oval, fill=P.overlay0)

    def _start_pulse(self):
        self._pulse_phase = 0.0
        self._do_pulse()

    def _stop_pulse(self):
        if self._pulse_job:
            try:
                self.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None

    def _do_pulse(self):
        """Smooth sinusoidal glow on the status dot while the bridge runs."""
        self._pulse_phase = (self._pulse_phase + 0.12) % (2 * math.pi)
        t = (math.sin(self._pulse_phase) + 1) / 2  # 0.0 to 1.0

        # Interpolate: dim green (#4a7a4a) <-> bright green (#a6e3a1)
        r = int(0x4a + t * (0xa6 - 0x4a))
        g = int(0x7a + t * (0xe3 - 0x7a))
        b = int(0x4a + t * (0xa1 - 0x4a))
        color = f"#{r:02x}{g:02x}{b:02x}"

        try:
            self._dot_canvas.itemconfigure(self._dot_oval, fill=color)
        except Exception:
            return

        self._pulse_job = self.after(40, self._do_pulse)

    # ── Bridge lifecycle ──────────────────────────────────────────────────────

    def _on_start(self):
        if self._runner and self._runner.is_running:
            return
        self._save_settings()
        config = self._collect_config()
        log.info("Starting bridge \u2014 %s @ %d baud \u2026",
                 config["serial_port"], config["baud_rate"])
        self._runner = _BridgeRunner(config)
        self._runner.start()
        self._set_running(True)
        self.after(800, self._watch_bridge)

    def _watch_bridge(self):
        if self._runner is None:
            return
        if not self._runner.is_running:
            err = self._runner.error
            self._set_running(False)
            self._runner = None
            if err:
                self._dot_canvas.itemconfigure(self._dot_oval, fill=P.red)
                self._status_label.configure(text="Error", text_color=P.red)
                messagebox.showerror(
                    "Bridge Error",
                    f"The bridge stopped unexpectedly:\n\n{err}",
                )
        else:
            self.after(1000, self._watch_bridge)

    def _on_stop(self):
        if not self._runner:
            return
        runner       = self._runner
        self._runner = None
        self._set_running(False, stopping=True)
        log.info("Stopping bridge \u2014 waiting for serial port to close \u2026")

        # Run the blocking wait in a daemon thread so the GUI stays responsive
        def _do_stop():
            runner.stop()
            self.after(0, lambda: self._set_running(False))

        threading.Thread(target=_do_stop, daemon=True, name="bridge-stop").start()

    def on_close(self):
        if self._runner and self._runner.is_running:
            if not messagebox.askokcancel(
                "Quit",
                "The bridge is running.\nStop it and quit?",
            ):
                return
            self._runner.stop()
            self._runner = None
        self._save_settings()
        self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler("AIbridge.log", encoding="utf-8")],
    )
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
