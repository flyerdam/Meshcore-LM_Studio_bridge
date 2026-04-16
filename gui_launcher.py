#!/usr/bin/env python3
"""
MeshCore-LM Studio Bridge — GUI Launcher
=========================================
Run this file instead of AIbridge.py to get a graphical interface.

    python gui_launcher.py

Requires only the packages that AIbridge.py already needs (tkinter is
part of the Python standard library on all major platforms).
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from types import SimpleNamespace

# Optional: list serial ports (pyserial is a meshcore dependency)
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

# ── Colour palette (Catppuccin Mocha) ────────────────────────────────────────
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

# ── Platform fonts ────────────────────────────────────────────────────────────
if sys.platform == "win32":
    _MONO   = ("Cascadia Code", 10)
    _SANS   = ("Segoe UI", 10)
    _SANS_S = ("Segoe UI", 9)
    _SANS_B = ("Segoe UI", 10, "bold")
    _SANS_H = ("Segoe UI", 12, "bold")
elif sys.platform == "darwin":
    _MONO   = ("Menlo", 10)
    _SANS   = ("SF Pro Text", 10)
    _SANS_S = ("SF Pro Text", 9)
    _SANS_B = ("SF Pro Text", 10, "bold")
    _SANS_H = ("SF Pro Text", 12, "bold")
else:
    _MONO   = ("DejaVu Sans Mono", 10)
    _SANS   = ("DejaVu Sans", 10)
    _SANS_S = ("DejaVu Sans", 9)
    _SANS_B = ("DejaVu Sans", 10, "bold")
    _SANS_H = ("DejaVu Sans", 12, "bold")

# ── Bot features that can be toggled on/off ───────────────────────────────────
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

# Where to persist settings between sessions
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "bridge_gui_config.json")


# ── Helpers ───────────────────────────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    """Forwards log records to a thread-safe queue for the GUI."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord):
        try:
            self._q.put_nowait(record)
        except queue.Full:
            pass


class _BridgeRunner(threading.Thread):
    """Runs :class:`MeshCoreLLMBridge` in a daemon thread + asyncio loop."""

    def __init__(self, config: dict):
        super().__init__(daemon=True, name="bridge-thread")
        self.config  = config
        self.error: Exception | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = threading.Event()

    # -- Thread entry ----------------------------------------------------------

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            log.error("Bridge crashed: %s", exc)
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._stopped.set()

    async def _async_main(self):
        bridge = MeshCoreLLMBridge(self.config)
        try:
            await bridge.run()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await bridge.serial.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # -- Control ---------------------------------------------------------------

    def stop(self):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._stopped.wait(timeout=8)

    @property
    def is_running(self) -> bool:
        return self.is_alive() and not self._stopped.is_set()


# ── Scrollable frame ──────────────────────────────────────────────────────────

class _ScrollableFrame(tk.Frame):
    """Vertical-scrollable container; scroll only when mouse is inside."""

    def __init__(self, parent, bg: str = P.base, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._vsb    = ttk.Scrollbar(self, orient="vertical",
                                     command=self._canvas.yview)
        self.inner   = tk.Frame(self._canvas, bg=bg)
        self._win_id = self._canvas.create_window((0, 0),
                                                  window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._vsb.set)

        self.inner.bind("<Configure>", self._update_scroll)
        self._canvas.bind("<Configure>", self._update_width)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._vsb.pack(side="right", fill="y")

        # Bind scroll only while cursor is within this panel
        self._canvas.bind("<Enter>", self._bind_scroll)
        self._canvas.bind("<Leave>", self._unbind_scroll)
        self.inner.bind("<Enter>", self._bind_scroll)
        self.inner.bind("<Leave>", self._unbind_scroll)

    def _update_scroll(self, _=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _update_width(self, event):
        self._canvas.itemconfigure(self._win_id, width=event.width)

    def _bind_scroll(self, _=None):
        self._canvas.bind_all("<MouseWheel>", self._on_scroll)
        self._canvas.bind_all("<Button-4>",   self._on_scroll)
        self._canvas.bind_all("<Button-5>",   self._on_scroll)

    def _unbind_scroll(self, _=None):
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_scroll(self, event):
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * event.delta / 120), "units")


# ── Main application ──────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("MeshCore AI Bridge")
        self.geometry("1200x780")
        self.minsize(900, 600)
        self.configure(bg=P.base)

        self._runner: _BridgeRunner | None = None
        self._log_queue: queue.Queue        = queue.Queue()
        self._log_formatter = logging.Formatter(
            "%(message)s", datefmt="%H:%M:%S"
        )

        # tkinter variable dicts
        self._vars:     dict[str, tk.Variable] = {}
        self._feat_vars: dict[str, tk.BooleanVar] = {}

        self._build_styles()
        self._build_ui()
        self._setup_logging()
        self._load_settings()
        self._poll_log()

    # ── Styles ────────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        # Global defaults
        s.configure(".", background=P.base, foreground=P.text,
                    fieldbackground=P.surface0, font=_SANS,
                    borderwidth=0, relief="flat", troughcolor=P.mantle)

        s.configure("TLabel",       background=P.base, foreground=P.text)
        s.configure("TFrame",       background=P.base)
        s.configure("TCheckbutton", background=P.base, foreground=P.text,
                    indicatorcolor=P.surface1, indicatorrelief="flat")
        s.map("TCheckbutton",
              background=[("active", P.surface0)],
              foreground=[("active", P.text)])

        s.configure("TEntry", fieldbackground=P.surface0, foreground=P.text,
                    insertcolor=P.text, padding=4)
        s.configure("TCombobox", fieldbackground=P.surface0,
                    background=P.surface0, foreground=P.text,
                    selectbackground=P.surface1, selectforeground=P.text,
                    padding=4)
        s.map("TCombobox",
              fieldbackground=[("readonly", P.surface0)],
              background=[("active", P.surface1)])

        s.configure("TSpinbox", fieldbackground=P.surface0,
                    foreground=P.text, arrowcolor=P.subtext,
                    insertcolor=P.text, padding=4)

        s.configure("TScrollbar", background=P.surface1,
                    troughcolor=P.mantle, arrowcolor=P.subtext)

        s.configure("TNotebook",     background=P.base)
        s.configure("TNotebook.Tab", background=P.surface0,
                    foreground=P.subtext, padding=[10, 4])
        s.map("TNotebook.Tab",
              background=[("selected", P.surface1)],
              foreground=[("selected", P.text)])

        s.configure("Section.TLabel",  background=P.base,
                    foreground=P.blue,  font=_SANS_H)
        s.configure("Key.TLabel",      background=P.base,
                    foreground=P.subtext, font=_SANS_S)
        s.configure("Hint.TLabel",     background=P.base,
                    foreground=P.overlay0, font=_SANS_S)

        s.configure("Start.TButton", background=P.green, foreground=P.base,
                    font=_SANS_B, padding=[20, 6])
        s.map("Start.TButton",
              background=[("active", "#b5ebaa"), ("disabled", P.surface1)],
              foreground=[("disabled", P.overlay0)])

        s.configure("Stop.TButton", background=P.red, foreground=P.base,
                    font=_SANS_B, padding=[20, 6])
        s.map("Stop.TButton",
              background=[("active", "#f7829a")])

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──────────────────────────────────────────────────────────
        top = tk.Frame(self, bg=P.mantle, height=52)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        tk.Label(top, text="🛰  MeshCore AI Bridge",
                 bg=P.mantle, fg=P.text,
                 font=((_SANS_H[0]), 14, "bold")).pack(side="left",
                                                       padx=16, pady=12)

        self._status_label = tk.Label(top, text="Stopped",
                                      bg=P.mantle, fg=P.overlay1,
                                      font=_SANS_S)
        self._status_label.pack(side="right", padx=8, pady=12)

        self._status_dot = tk.Label(top, text="●", bg=P.mantle,
                                    fg=P.overlay0, font=(_SANS[0], 20))
        self._status_dot.pack(side="right", padx=(0, 4), pady=12)

        # ── Horizontal pane ───────────────────────────────────────────────────
        pane = tk.PanedWindow(self, orient="horizontal",
                              bg=P.surface0, sashwidth=4,
                              sashrelief="flat", bd=0)
        pane.pack(fill="both", expand=True)

        # Left — settings
        left_outer = tk.Frame(pane, bg=P.base)
        sf = _ScrollableFrame(left_outer, bg=P.base)
        sf.pack(fill="both", expand=True)
        self._settings_inner = sf.inner
        pane.add(left_outer, minsize=340, width=420)

        # Right — console
        right = tk.Frame(pane, bg=P.crust)
        pane.add(right, minsize=400)
        self._build_console(right)

        # Populate settings widgets
        self._build_settings()

        # ── Bottom bar ────────────────────────────────────────────────────────
        bot = tk.Frame(self, bg=P.crust, height=46)
        bot.pack(fill="x", side="bottom")
        bot.pack_propagate(False)

        self._start_btn = ttk.Button(bot, text="▶  Start",
                                     style="Start.TButton",
                                     command=self._on_start)
        self._start_btn.pack(side="left", padx=12, pady=7)

        self._stop_btn = ttk.Button(bot, text="■  Stop",
                                    style="Stop.TButton",
                                    command=self._on_stop,
                                    state="disabled")
        self._stop_btn.pack(side="left", padx=4, pady=7)

        self._bottom_msg = tk.Label(bot,
                                    text="Configure settings and press  ▶ Start.",
                                    bg=P.crust, fg=P.subtext, font=_SANS_S)
        self._bottom_msg.pack(side="left", padx=14)

    # ── Console ───────────────────────────────────────────────────────────────

    def _build_console(self, parent: tk.Frame):
        hdr = tk.Frame(parent, bg=P.mantle)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Console", bg=P.mantle, fg=P.blue,
                 font=_SANS_B).pack(side="left", padx=12, pady=6)

        ttk.Button(hdr, text="Clear", command=self._clear_console).pack(
            side="right", padx=8, pady=4)

        # Text widget + scrollbar
        txt_frame = tk.Frame(parent, bg=P.crust)
        txt_frame.pack(fill="both", expand=True)

        self._console = tk.Text(
            txt_frame,
            bg=P.crust, fg=P.text,
            font=_MONO,
            state="disabled",
            wrap="word",
            relief="flat", bd=0,
            padx=10, pady=8,
            insertbackground=P.text,
        )
        vsb = ttk.Scrollbar(txt_frame, orient="vertical",
                            command=self._console.yview)
        self._console.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._console.pack(fill="both", expand=True)

        # Colour tags
        self._console.tag_configure("TS",       foreground=P.overlay0)
        self._console.tag_configure("INFO",     foreground=P.text)
        self._console.tag_configure("DEBUG",    foreground=P.overlay1)
        self._console.tag_configure("WARNING",  foreground=P.yellow)
        self._console.tag_configure("ERROR",    foreground=P.red)
        self._console.tag_configure("CRITICAL", foreground=P.red,
                                    underline=True)
        self._console.tag_configure("RECV",     foreground=P.sky)
        self._console.tag_configure("SEND",     foreground=P.green)
        self._console.tag_configure("LLM",      foreground=P.mauve)
        self._console.tag_configure("BOT",      foreground=P.peach)
        self._console.tag_configure("CONN",     foreground=P.teal)

    def _clear_console(self):
        self._console.config(state="normal")
        self._console.delete("1.0", "end")
        self._console.config(state="disabled")

    # ── Settings panel ────────────────────────────────────────────────────────

    def _build_settings(self):
        p = self._settings_inner  # shorthand

        # ── helpers ──────────────────────────────────────────────────────────
        def section(icon: str, title: str):
            f = tk.Frame(p, bg=P.base)
            f.pack(fill="x", padx=12, pady=(14, 3))
            tk.Label(f, text=f"{icon}  {title}",
                     bg=P.base, fg=P.blue,
                     font=_SANS_H).pack(side="left")
            tk.Frame(f, bg=P.surface1, height=1).pack(
                side="left", fill="x", expand=True, padx=(8, 0), pady=3)

        def field(label: str, widget_fn, **kw):
            """One label + widget row inside the settings panel."""
            row = tk.Frame(p, bg=P.base)
            row.pack(fill="x", padx=20, pady=2)
            tk.Label(row, text=label, bg=P.base, fg=P.subtext,
                     font=_SANS_S, width=24, anchor="w").pack(side="left")
            w = widget_fn(row, **kw)
            w.pack(side="left", fill="x", expand=True, padx=(4, 0))
            return w

        def hint(text: str):
            tk.Label(p, text=f"   {text}", bg=P.base, fg=P.overlay0,
                     font=_SANS_S).pack(anchor="w", padx=32)

        D = DEFAULT_CONFIG

        # ── Connection ───────────────────────────────────────────────────────
        section("⚡", "Connection")

        self._vars["serial_port"] = tk.StringVar(value=D["serial_port"])
        ports = _get_serial_ports() or [D["serial_port"]]

        port_row = tk.Frame(p, bg=P.base)
        port_row.pack(fill="x", padx=20, pady=2)
        tk.Label(port_row, text="Serial Port", bg=P.base, fg=P.subtext,
                 font=_SANS_S, width=24, anchor="w").pack(side="left")
        self._port_cb = ttk.Combobox(port_row,
                                     textvariable=self._vars["serial_port"],
                                     values=ports, width=18)
        self._port_cb.pack(side="left", padx=(4, 0))
        tk.Button(port_row, text="↻", bg=P.surface0, fg=P.text,
                  font=_SANS, relief="flat", bd=0, padx=6,
                  activebackground=P.surface1, activeforeground=P.text,
                  command=self._refresh_ports).pack(side="left", padx=(4, 0))

        self._vars["baud_rate"] = tk.IntVar(value=D["baud_rate"])
        field("Baud Rate", ttk.Spinbox,
              textvariable=self._vars["baud_rate"],
              from_=1200, to=921600, increment=9600, width=10)

        # ── LM Studio ────────────────────────────────────────────────────────
        section("🤖", "LM Studio")

        self._vars["lm_url"] = tk.StringVar(value=D["lm_url"])
        field("API URL", ttk.Entry, textvariable=self._vars["lm_url"])

        self._vars["model"] = tk.StringVar(value=D["model"])
        field("Model Name", ttk.Entry, textvariable=self._vars["model"])

        # ── Commands ─────────────────────────────────────────────────────────
        section("💬", "Commands")

        self._vars["ai_prefix"]  = tk.StringVar(value=D["ai_prefix"])
        self._vars["bot_prefix"] = tk.StringVar(value=D["bot_prefix"])
        field("AI Prefix",  ttk.Entry, textvariable=self._vars["ai_prefix"],  width=10)
        field("Bot Prefix", ttk.Entry, textvariable=self._vars["bot_prefix"], width=10)

        # ── Channels ─────────────────────────────────────────────────────────
        section("📡", "Channels")

        self._vars["listen_channels"] = tk.StringVar(value="")
        self._vars["reply_channel"]   = tk.StringVar(value="")
        field("Listen Channels", ttk.Entry,
              textvariable=self._vars["listen_channels"])
        hint("blank = all channels  |  e.g.: 0 2 3")
        field("Reply Channel", ttk.Entry,
              textvariable=self._vars["reply_channel"])
        hint("blank = same channel as question")

        # ── Timing ───────────────────────────────────────────────────────────
        section("⏱", "Timing")

        self._vars["message_cooldown_s"]  = tk.DoubleVar(value=0.0)
        self._vars["reply_delay_s"]       = tk.DoubleVar(value=D["reply_delay_s"])
        self._vars["telemetry_interval_s"]= tk.IntVar(value=D["telemetry_interval_s"])
        self._vars["monitor_reminder_s"]  = tk.IntVar(value=D["monitor_reminder_s"])
        self._vars["channel_context_msgs"]= tk.IntVar(value=D["channel_context_msgs"])

        field("Message Cooldown (s)", ttk.Spinbox,
              textvariable=self._vars["message_cooldown_s"],
              from_=0, to=300, increment=1, width=8)
        hint("seconds a user must wait between commands  (0 = off)")

        field("Reply Delay (s)", ttk.Spinbox,
              textvariable=self._vars["reply_delay_s"],
              from_=0.0, to=10.0, increment=0.1, width=8)
        field("Telemetry Interval (s)", ttk.Spinbox,
              textvariable=self._vars["telemetry_interval_s"],
              from_=30, to=3600, increment=30, width=8)
        field("Monitor Reminder (s)", ttk.Spinbox,
              textvariable=self._vars["monitor_reminder_s"],
              from_=60, to=7200, increment=60, width=8)
        field("Channel Context (msgs)", ttk.Spinbox,
              textvariable=self._vars["channel_context_msgs"],
              from_=0, to=50, increment=1, width=8)
        hint("recent messages injected into AI context  (0 = disabled)")

        # ── Integrations ─────────────────────────────────────────────────────
        section("🔗", "Integrations")

        self._vars["news_api_key"] = tk.StringVar(value="")
        field("News API Key", ttk.Entry,
              textvariable=self._vars["news_api_key"], show="*")
        hint("newsapi.org free key  (leave blank for DuckDuckGo only)")

        # ── Features ─────────────────────────────────────────────────────────
        section("🧩", "Features")

        # AI toggle (full block)
        ai_block = tk.Frame(p, bg=P.surface0, padx=8, pady=6)
        ai_block.pack(fill="x", padx=12, pady=(4, 2))
        self._feat_vars["__ai__"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(ai_block,
                        text="🤖  AI Queries  (responds to the !ai prefix)",
                        variable=self._feat_vars["__ai__"]).pack(anchor="w")

        # Bot command grid (2 columns)
        tk.Label(p, text="  Bot Commands:",
                 bg=P.base, fg=P.subtext,
                 font=_SANS_S).pack(anchor="w", padx=20, pady=(8, 2))

        grid = tk.Frame(p, bg=P.base)
        grid.pack(fill="x", padx=12, pady=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        for i, (key, label) in enumerate(BOT_FEATURES):
            self._feat_vars[key] = tk.BooleanVar(value=True)
            cell = tk.Frame(grid, bg=P.surface0, padx=6, pady=4)
            cell.grid(row=i // 2, column=i % 2, sticky="ew",
                      padx=2, pady=1)
            ttk.Checkbutton(cell, text=label,
                            variable=self._feat_vars[key]).pack(anchor="w")

        # Bottom padding
        tk.Frame(p, bg=P.base, height=24).pack()

    def _refresh_ports(self):
        ports = _get_serial_ports()
        if ports:
            self._port_cb["values"] = ports

    # ── Logging ───────────────────────────────────────────────────────────────

    def _setup_logging(self):
        handler = _QueueHandler(self._log_queue)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        # Keep a StreamHandler for terminal visibility too
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
        import datetime
        ts  = datetime.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg = record.getMessage()

        # Choose colour tag from level, then refine by content
        level = record.levelname            # INFO / DEBUG / WARNING / ERROR / CRITICAL
        tag   = level

        lc = msg.lower()
        if "<<" in msg and ("ch" in lc or "direct" in lc):
            tag = "RECV"
        elif ">>" in msg:
            tag = "SEND"
        elif "lm studio" in lc or "llm" in lc or "ai trigger" in lc:
            tag = "LLM"
        elif "bot cmd" in lc:
            tag = "BOT"
        elif any(kw in lc for kw in ("serial", "connect", "reconnect", "disconnect")):
            tag = "CONN"

        self._console.config(state="normal")
        at_end = self._console.yview()[1] >= 0.98
        self._console.insert("end", f"[{ts}] ", "TS")
        self._console.insert("end", f"{msg}\n", tag)
        if at_end:
            self._console.see("end")
        self._console.config(state="disabled")

    # ── Settings persistence ──────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        """Read all widget values and return a bridge config dict."""
        def _get(key, fallback=None):
            var = self._vars.get(key)
            if var is None:
                return fallback
            try:
                return var.get()
            except Exception:  # noqa: BLE001
                return fallback

        config = DEFAULT_CONFIG.copy()

        config["serial_port"]          = _get("serial_port")   or DEFAULT_CONFIG["serial_port"]
        config["baud_rate"]            = int(_get("baud_rate",  DEFAULT_CONFIG["baud_rate"]))
        config["lm_url"]               = _get("lm_url")        or DEFAULT_CONFIG["lm_url"]
        config["model"]                = _get("model")         or DEFAULT_CONFIG["model"]
        config["ai_prefix"]            = _get("ai_prefix")     or DEFAULT_CONFIG["ai_prefix"]
        config["bot_prefix"]           = _get("bot_prefix")    or DEFAULT_CONFIG["bot_prefix"]
        config["message_cooldown_s"]   = float(_get("message_cooldown_s", 0))
        config["reply_delay_s"]        = float(_get("reply_delay_s",
                                                    DEFAULT_CONFIG["reply_delay_s"]))
        config["telemetry_interval_s"] = int(_get("telemetry_interval_s",
                                                   DEFAULT_CONFIG["telemetry_interval_s"]))
        config["monitor_reminder_s"]   = int(_get("monitor_reminder_s",
                                                   DEFAULT_CONFIG["monitor_reminder_s"]))
        config["channel_context_msgs"] = int(_get("channel_context_msgs",
                                                   DEFAULT_CONFIG["channel_context_msgs"]))
        config["news_api_key"]         = _get("news_api_key") or None

        # Channels
        listen_raw = (_get("listen_channels") or "").strip()
        if listen_raw:
            try:
                config["listen_channels"] = [int(x) for x in listen_raw.split()]
            except ValueError:
                config["listen_channels"] = None
        else:
            config["listen_channels"] = None

        reply_raw = (_get("reply_channel") or "").strip()
        if reply_raw:
            try:
                config["reply_channel"] = int(reply_raw)
            except ValueError:
                config["reply_channel"] = None
        else:
            config["reply_channel"] = None

        # Feature toggles
        config["ai_enabled"]      = self._feat_vars.get(
            "__ai__", tk.BooleanVar(value=True)).get()
        disabled: set[str] = set()
        for key, _ in BOT_FEATURES:
            if not self._feat_vars.get(key, tk.BooleanVar(value=True)).get():
                disabled.add(key)
        config["disabled_commands"] = disabled

        return config

    def _save_settings(self):
        """Persist UI values to JSON."""
        data: dict = {}
        for key, var in self._vars.items():
            try:
                data[key] = var.get()
            except Exception:  # noqa: BLE001
                pass
        for key, var in self._feat_vars.items():
            try:
                data[f"feat_{key}"] = var.get()
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            log.debug("Could not save settings: %s", exc)

    def _load_settings(self):
        """Restore persisted UI values from JSON."""
        if not os.path.exists(_CONFIG_FILE):
            return
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not load settings: %s", exc)
            return

        for key, var in self._vars.items():
            if key in data:
                try:
                    var.set(data[key])
                except Exception:  # noqa: BLE001
                    pass
        for key, var in self._feat_vars.items():
            saved_key = f"feat_{key}"
            if saved_key in data:
                try:
                    var.set(bool(data[saved_key]))
                except Exception:  # noqa: BLE001
                    pass

    # ── Bridge lifecycle ──────────────────────────────────────────────────────

    def _set_running(self, running: bool):
        if running:
            self._start_btn.config(state="disabled")
            self._stop_btn.config(state="normal")
            self._status_dot.config(fg=P.green)
            self._status_label.config(text="Running", fg=P.green)
            self._bottom_msg.config(text="Bridge is running — watch the console for activity.")
        else:
            self._start_btn.config(state="normal")
            self._stop_btn.config(state="disabled")
            self._status_dot.config(fg=P.overlay0)
            self._status_label.config(text="Stopped", fg=P.overlay1)
            self._bottom_msg.config(text="Bridge stopped.  Press  ▶ Start  to launch again.")

    def _on_start(self):
        if self._runner and self._runner.is_running:
            return
        self._save_settings()
        config = self._collect_config()
        log.info("Starting bridge — %s @ %d baud …",
                 config["serial_port"], config["baud_rate"])
        self._runner = _BridgeRunner(config)
        self._runner.start()
        self._set_running(True)
        self.after(800, self._watch_bridge)

    def _watch_bridge(self):
        """Periodically check if the bridge thread is still alive."""
        if self._runner is None:
            return
        if not self._runner.is_running:
            err = self._runner.error
            self._set_running(False)
            self._runner = None
            if err:
                self._status_dot.config(fg=P.red)
                self._status_label.config(text="Error", fg=P.red)
                messagebox.showerror(
                    "Bridge Error",
                    f"The bridge stopped unexpectedly:\n\n{err}",
                )
        else:
            self.after(1000, self._watch_bridge)

    def _on_stop(self):
        if self._runner:
            log.info("Stopping bridge …")
            self._runner.stop()
            self._runner = None
        self._set_running(False)

    def on_close(self):
        if self._runner and self._runner.is_running:
            if not messagebox.askokcancel(
                "Quit",
                "The bridge is running.\nStop it and quit?",
            ):
                return
            self._runner.stop()
        self._save_settings()
        self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler("AIbridge.log",
                                                      encoding="utf-8")])
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
