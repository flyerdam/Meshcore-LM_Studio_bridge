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
import time
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

# Optional: OSM map widget
try:
    import tkintermapview
    from PIL import Image, ImageDraw, ImageTk as _ImageTk
    _HAS_MAPVIEW = True
except ImportError:
    _HAS_MAPVIEW = False

_TILE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tile_cache.db")
_marker_icon_cache: dict = {}  # color_hex -> tk.PhotoImage (kept alive here)


def _make_marker_icon(color_hex: str, size: int = 30) -> "tk.PhotoImage":
    """Create a glossy circular marker icon as a tkinter PhotoImage."""
    try:
        r, g, b = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
    except Exception:
        r, g, b = 200, 200, 200
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Drop shadow
    draw.ellipse([2, 3, size - 1, size], fill=(0, 0, 0, 90))
    # Black outline
    draw.ellipse([0, 0, size - 4, size - 4], fill=(20, 20, 20, 255))
    # Colored fill
    inset = 3
    draw.ellipse([inset, inset, size - 4 - inset, size - 4 - inset], fill=(r, g, b, 255))
    # Glossy highlight top-left
    hi = max(4, size // 5)
    draw.ellipse([inset + 2, inset + 2, inset + 2 + hi, inset + 2 + hi],
                 fill=(255, 255, 255, 140))
    return _ImageTk.PhotoImage(img)

from meshcore_bridge.config import (
    DEFAULT_CONFIG,
    DEFAULT_GITHUB_MODELS_URL,
    DEFAULT_OPENAI_COMPAT_URL,
    load_persisted_config,
    save_user_config,
)
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
_STOP_TIMEOUT       = 10            # seconds to wait for clean shutdown
_DISCOVERIES_FILE   = "discoveries.json"


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

    def __init__(self, config: dict, map_queue: queue.Queue):
        super().__init__(daemon=True, name="bridge-thread")
        self.config = config
        self._map_queue = map_queue
        self.error: Exception | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopped = threading.Event()
        self._bridge: "MeshCoreLLMBridge | None" = None

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
        bridge = MeshCoreLLMBridge(self.config, map_queue=self._map_queue)
        self._bridge = bridge
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

    def submit(self, coro) -> "concurrent.futures.Future | None":
        """Schedule a coroutine on the bridge asyncio loop; returns a Future."""
        import concurrent.futures
        if self._loop is None or self._loop.is_closed():
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

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


# ── API Keys popup ────────────────────────────────────────────────────────────

class _APIKeysDialog(ctk.CTkToplevel):
    """Modal popup for editing provider API keys."""

    def __init__(self, parent, provider_vars: dict[str, ctk.StringVar]):
        super().__init__(parent)
        self.title("API Keys")
        self.geometry("480x430")
        self.resizable(False, False)
        self.grab_set()  # modal
        self.focus_force()
        self.configure(fg_color=P.mantle)
        self._vars = provider_vars
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Provider API Keys",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=P.text).pack(pady=(16, 4), padx=20, anchor="w")
        ctk.CTkLabel(self, text="Stored in bridge_config.user.json (gitignored). Prefer env vars — never written to disk.",
                     font=ctk.CTkFont(size=10), text_color=P.overlay0).pack(padx=20, anchor="w")

        frame = ctk.CTkFrame(self, fg_color=P.base, corner_radius=8)
        frame.pack(fill="both", expand=True, padx=16, pady=12)
        frame.grid_columnconfigure(1, weight=1)

        labels = {
            "llm_api_key":      ("Main LLM",   "env: GITHUB_TOKEN / LLM_API_KEY / OPENAI_API_KEY"),
            "local_gate_api_key": ("Gate LLM", "Leave blank for local servers (LM Studio, Ollama)"),
            "news_api_key":     ("NewsAPI",    "newsapi.org key — env: NEWS_API_KEY"),
        }
        for row_idx, (key, (label, hint)) in enumerate(labels.items()):
            ctk.CTkLabel(frame, text=label, text_color=P.subtext,
                         font=ctk.CTkFont(size=12),
                         width=100, anchor="w").grid(row=row_idx * 2, column=0,
                                                      padx=12, pady=(10, 2), sticky="w")
            var = self._vars.get(key, ctk.StringVar())
            entry = ctk.CTkEntry(frame, textvariable=var, show="●",
                                 fg_color=P.surface0, text_color=P.text,
                                 border_width=0, height=32)
            entry.grid(row=row_idx * 2, column=1, padx=(4, 12), pady=(10, 2), sticky="ew")
            ctk.CTkLabel(frame, text=f"   {hint}",
                         text_color=P.overlay0,
                         font=ctk.CTkFont(size=10),
                         wraplength=300, justify="left").grid(
                row=row_idx * 2 + 1, column=0, columnspan=2,
                padx=12, pady=(0, 4), sticky="w")

        ctk.CTkButton(self, text="Close", width=100, height=32,
                      fg_color=P.blue, hover_color=P.sky, text_color=P.base,
                      command=self.destroy).pack(pady=(0, 14))


# ── Channels manager popup ────────────────────────────────────────────────────

class _ChannelsDialog(ctk.CTkToplevel):
    """
    Modal dialog that reads channels from the connected device, lets the user
    choose which ones the bridge should listen to, and can write new / updated
    channel slots back to the hardware.
    """

    _MAX_SLOTS = 8  # safe default; overridden by max_channels from device_info

    def __init__(self, parent, runner: "_BridgeRunner | None",
                 listen_channels_var: ctk.StringVar):
        super().__init__(parent)
        self.title("Channel Manager")
        self.geometry("540x700")
        self.resizable(True, True)
        self.grab_set()
        self.focus_force()
        self.configure(fg_color=P.mantle)

        self._runner = runner
        self._listen_var = listen_channels_var
        self._channel_check_vars: list[ctk.BooleanVar] = []
        self._channel_rows: list[dict] = []  # {idx, name, hash}

        self._build()
        self._populate_listen_filter()

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build(self):
        # Title
        ctk.CTkLabel(self, text="📡  Channel Manager",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=P.text).pack(pady=(14, 2), padx=20, anchor="w")
        ctk.CTkLabel(self,
                     text="Fetch channels from device  →  tick which to listen  →  Apply Filter",
                     font=ctk.CTkFont(size=10), text_color=P.overlay0).pack(padx=20, anchor="w")

        # ── Fetch button ──
        fetch_frame = ctk.CTkFrame(self, fg_color="transparent")
        fetch_frame.pack(fill="x", padx=16, pady=(8, 4))
        self._fetch_btn = ctk.CTkButton(
            fetch_frame, text="🔄  Fetch from device", width=180, height=30,
            fg_color=P.blue, hover_color=P.sky, text_color=P.base,
            command=self._on_fetch,
            state="normal" if (self._runner and self._runner.is_running) else "disabled",
        )
        self._fetch_btn.pack(side="left")
        self._fetch_status = ctk.CTkLabel(fetch_frame, text="" if (self._runner and self._runner.is_running)
                                           else "  Bridge not running — fetch unavailable",
                                          text_color=P.overlay0, font=ctk.CTkFont(size=10))
        self._fetch_status.pack(side="left", padx=8)

        # ── Channel list ──
        ctk.CTkLabel(self, text="Channels  (✓ = bridge will listen on this channel)",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=P.subtext).pack(padx=16, pady=(8, 2), anchor="w")

        list_outer = ctk.CTkFrame(self, fg_color=P.base, corner_radius=8, height=220)
        list_outer.pack(fill="x", expand=False, padx=16, pady=(0, 4))
        list_outer.pack_propagate(False)

        self._list_frame = ctk.CTkScrollableFrame(list_outer, fg_color="transparent")
        self._list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self._list_frame.grid_columnconfigure(2, weight=1)

        # Seed with placeholder rows 0..7 until fetched
        self._render_placeholder_rows()

        # ── Apply filter button ──
        ctk.CTkButton(self, text="✅  Apply Filter", width=160, height=30,
                      fg_color=P.green, hover_color=P.teal, text_color=P.base,
                      command=self._apply_filter).pack(pady=(2, 4))

        # ── Set / Add channel section ──
        sep = ctk.CTkFrame(self, fg_color=P.surface0, height=1)
        sep.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(self, text="Set / Add Channel on Device",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=P.subtext).pack(padx=16, pady=(4, 2), anchor="w")

        edit_frame = ctk.CTkFrame(self, fg_color=P.base, corner_radius=8)
        edit_frame.pack(fill="x", padx=16, pady=(0, 4))
        edit_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(edit_frame, text="Slot #", text_color=P.subtext,
                     font=ctk.CTkFont(size=11), width=80, anchor="w"
                     ).grid(row=0, column=0, padx=10, pady=6, sticky="w")
        self._slot_var = ctk.StringVar(value="0")
        ctk.CTkEntry(edit_frame, textvariable=self._slot_var, width=60,
                     fg_color=P.surface0, text_color=P.text, border_width=0
                     ).grid(row=0, column=1, padx=(4, 10), pady=6, sticky="w")

        ctk.CTkLabel(edit_frame, text="Name", text_color=P.subtext,
                     font=ctk.CTkFont(size=11), width=80, anchor="w"
                     ).grid(row=1, column=0, padx=10, pady=6, sticky="w")
        self._name_var = ctk.StringVar()
        ctk.CTkEntry(edit_frame, textvariable=self._name_var,
                     placeholder_text="#ChannelName  or  MyChannel",
                     fg_color=P.surface0, text_color=P.text, border_width=0
                     ).grid(row=1, column=1, padx=(4, 10), pady=6, sticky="ew")

        ctk.CTkLabel(edit_frame, text="Secret key\n(hex, optional)", text_color=P.subtext,
                     font=ctk.CTkFont(size=10), width=80, anchor="w"
                     ).grid(row=2, column=0, padx=10, pady=6, sticky="w")
        self._secret_var = ctk.StringVar()
        ctk.CTkEntry(edit_frame, textvariable=self._secret_var,
                     placeholder_text="32 hex chars — leave blank to derive from name",
                     fg_color=P.surface0, text_color=P.text, border_width=0
                     ).grid(row=2, column=1, padx=(4, 10), pady=6, sticky="ew")

        ctk.CTkLabel(edit_frame, text="  # prefix = key derived from name  |  private = provide 32 hex chars",
                     text_color=P.overlay0, font=ctk.CTkFont(size=9),
                     ).grid(row=3, column=0, columnspan=2, padx=10, pady=(0, 4), sticky="w")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(2, 10))
        self._save_btn = ctk.CTkButton(
            btn_row, text="💾  Save to Device", width=160, height=30,
            fg_color=P.mauve, hover_color=P.peach, text_color=P.base,
            command=self._on_save_channel,
            state="normal" if (self._runner and self._runner.is_running) else "disabled",
        )
        self._save_btn.pack(side="left")
        self._save_status = ctk.CTkLabel(btn_row, text="", text_color=P.overlay0,
                                          font=ctk.CTkFont(size=10))
        self._save_status.pack(side="left", padx=8)

        ctk.CTkButton(btn_row, text="Close", width=80, height=30,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, command=self.destroy).pack(side="right")

    # ── Row rendering ─────────────────────────────────────────────────────────

    def _render_placeholder_rows(self):
        # Pre-populate with slot numbers, empty names
        self._channel_rows = [{"idx": i, "name": "", "hash": ""} for i in range(self._MAX_SLOTS)]
        self._render_rows()

    def _render_rows(self):
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._channel_check_vars.clear()

        # Current listen set (to pre-check boxes)
        listen_set = self._current_listen_set()

        for i, ch in enumerate(self._channel_rows):
            idx = ch["idx"]
            check = ctk.BooleanVar(value=(listen_set is None or idx in listen_set))
            self._channel_check_vars.append(check)

            ctk.CTkCheckBox(
                self._list_frame, variable=check, text="",
                width=20, checkbox_width=18, checkbox_height=18,
                fg_color=P.blue, hover_color=P.sky, border_color=P.overlay0,
            ).grid(row=i, column=0, padx=(4, 0), pady=3, sticky="w")

            ctk.CTkLabel(self._list_frame,
                         text=f"#{idx}",
                         font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
                         text_color=P.blue, width=30, anchor="e",
                         ).grid(row=i, column=1, padx=(4, 4), pady=3, sticky="e")

            name_text = ch["name"] if ch["name"] else "(empty slot)"
            name_color = P.text if ch["name"] else P.overlay0
            lbl = ctk.CTkLabel(self._list_frame, text=name_text,
                               font=ctk.CTkFont(family="Consolas", size=11),
                               text_color=name_color, anchor="w")
            lbl.grid(row=i, column=2, padx=(2, 4), pady=3, sticky="ew")
            # Click label to pre-fill the edit form
            lbl.bind("<Button-1>", lambda e, ch=ch: self._prefill_edit(ch))

            hash_text = f"  [{ch['hash']}]" if ch.get("hash") else ""
            ctk.CTkLabel(self._list_frame, text=hash_text,
                         font=ctk.CTkFont(family="Consolas", size=10),
                         text_color=P.overlay0, anchor="w", width=40,
                         ).grid(row=i, column=3, padx=(0, 8), pady=3, sticky="w")

    def _prefill_edit(self, ch: dict):
        self._slot_var.set(str(ch["idx"]))
        self._name_var.set(ch["name"])
        self._secret_var.set("")

    def _current_listen_set(self) -> "set[int] | None":
        raw = (self._listen_var.get() or "").strip()
        if not raw or raw.lower() in {"none", "null", "all", "[]"}:
            return None  # all channels
        parts = [x for x in raw.split() if x.isdigit()]
        return set(int(x) for x in parts) if parts else None

    def _populate_listen_filter(self):
        """Pre-check boxes from the current listen_channels setting."""
        self._render_rows()

    # ── Fetch from device ─────────────────────────────────────────────────────

    def _on_fetch(self):
        if not self._runner or not self._runner.is_running:
            self._fetch_status.configure(text="  Bridge not running")
            return
        self._fetch_btn.configure(state="disabled", text="⏳  Fetching…")
        self._fetch_status.configure(text="")

        import threading as _t
        _t.Thread(target=self._fetch_thread, daemon=True).start()

    def _fetch_thread(self):
        try:
            bridge = self._runner._bridge
            if bridge is None:
                self.after(0, lambda: self._fetch_done(None, "Bridge not ready"))
                return
            max_slots = int(bridge._telemetry.get("max_channels") or self._MAX_SLOTS)

            # Fetch sequentially in a single coroutine — firing all at once causes
            # every request to receive slot 0's CHANNEL_INFO event (serial is shared).
            async def _fetch_all():
                results = []
                for i in range(max_slots):
                    try:
                        evt = await bridge.serial.execute(
                            lambda mc, i=i: mc.commands.get_channel(i)
                        )
                        if evt and hasattr(evt, "payload"):
                            p = evt.payload
                            results.append({
                                "idx":  p.get("channel_idx", i),
                                "name": p.get("channel_name", ""),
                                "hash": p.get("channel_hash", ""),
                            })
                        else:
                            results.append({"idx": i, "name": "", "hash": ""})
                    except Exception:
                        results.append({"idx": i, "name": "", "hash": ""})
                return results

            fut = self._runner.submit(_fetch_all())
            channels = fut.result(timeout=max_slots * 3.0)
            self.after(0, lambda: self._fetch_done(channels, None))
        except Exception as exc:
            self.after(0, lambda: self._fetch_done(None, str(exc)))

    def _fetch_done(self, channels: list | None, error: str | None):
        self._fetch_btn.configure(state="normal", text="🔄  Fetch from device")
        if error:
            self._fetch_status.configure(text=f"  Error: {error}", text_color=P.red)
            return
        self._fetch_status.configure(text=f"  Loaded {len(channels)} slots", text_color=P.green)
        self._channel_rows = channels
        self._render_rows()

    # ── Apply filter ──────────────────────────────────────────────────────────

    def _apply_filter(self):
        checked = [self._channel_rows[i]["idx"]
                   for i, var in enumerate(self._channel_check_vars) if var.get()]
        total = len(self._channel_check_vars)
        if len(checked) == total:
            # All checked = listen to all
            self._listen_var.set("")
        else:
            self._listen_var.set(" ".join(str(x) for x in sorted(checked)))
        self.destroy()

    # ── Save channel to device ────────────────────────────────────────────────

    def _on_save_channel(self):
        if not self._runner or not self._runner.is_running:
            self._save_status.configure(text="  Bridge not running", text_color=P.red)
            return

        slot_raw = self._slot_var.get().strip()
        name = self._name_var.get().strip()
        secret_hex = self._secret_var.get().strip()

        if not slot_raw.isdigit():
            self._save_status.configure(text="  Slot must be a number", text_color=P.red)
            return
        if not name:
            self._save_status.configure(text="  Name required", text_color=P.red)
            return
        if secret_hex and len(secret_hex.replace(" ", "")) != 32:
            self._save_status.configure(text="  Secret must be 32 hex chars (16 bytes)", text_color=P.red)
            return
        try:
            secret_bytes = bytes.fromhex(secret_hex.replace(" ", "")) if secret_hex else None
        except ValueError:
            self._save_status.configure(text="  Invalid hex in secret", text_color=P.red)
            return

        idx = int(slot_raw)
        self._save_btn.configure(state="disabled", text="⏳  Saving…")
        self._save_status.configure(text="")

        import threading as _t
        _t.Thread(target=self._save_thread, args=(idx, name, secret_bytes), daemon=True).start()

    def _save_thread(self, idx: int, name: str, secret: bytes | None):
        try:
            bridge = self._runner._bridge
            if bridge is None:
                self.after(0, lambda: self._save_done(False, "Bridge not ready"))
                return
            fut = self._runner.submit(
                bridge.serial.execute(lambda mc: mc.commands.set_channel(idx, name, secret))
            )
            evt = fut.result(timeout=6.0)
            ok = evt is not None and getattr(evt, "type", None) is not None
            err_msg = None if ok else "Device returned error"
            self.after(0, lambda: self._save_done(ok, err_msg))
        except Exception as exc:
            self.after(0, lambda: self._save_done(False, str(exc)))

    def _save_done(self, success: bool, error: str | None):
        self._save_btn.configure(state="normal", text="💾  Save to Device")
        if success:
            self._save_status.configure(text="  ✓ Saved!", text_color=P.green)
            # Re-fetch to show updated name
            self._on_fetch()
        else:
            self._save_status.configure(text=f"  Error: {error}", text_color=P.red)


# ── Add / Edit Provider popup ─────────────────────────────────────────────────

class _AddProviderDialog(ctk.CTkToplevel):
    """Modal popup for adding a custom provider preset."""

    def __init__(self, parent, on_save):
        super().__init__(parent)
        self.title("Add Provider")
        self.geometry("480x340")
        self.resizable(False, False)
        self.grab_set()
        self.focus_force()
        self.configure(fg_color=P.mantle)
        self._on_save = on_save
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Add Custom Provider",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=P.text).pack(pady=(16, 4), padx=20, anchor="w")
        ctk.CTkLabel(self, text="Saved as a preset — appears in the Provider dropdown",
                     font=ctk.CTkFont(size=10), text_color=P.overlay0).pack(padx=20, anchor="w")

        frame = ctk.CTkFrame(self, fg_color=P.base, corner_radius=8)
        frame.pack(fill="both", expand=True, padx=16, pady=12)
        frame.grid_columnconfigure(1, weight=1)

        fields = [
            ("name",     "Display Name",  "e.g. Ollama",                          False),
            ("url",      "API URL",       "e.g. http://localhost:11434/v1/chat/completions", False),
            ("model",    "Default Model", "e.g. llama3",                          False),
        ]
        self._field_vars: dict[str, ctk.StringVar] = {}
        for row_idx, (key, label, placeholder, masked) in enumerate(fields):
            ctk.CTkLabel(frame, text=label, text_color=P.subtext,
                         font=ctk.CTkFont(size=12),
                         width=120, anchor="w").grid(row=row_idx, column=0,
                                                      padx=12, pady=8, sticky="w")
            var = ctk.StringVar()
            self._field_vars[key] = var
            entry = ctk.CTkEntry(frame, textvariable=var,
                                 show="●" if masked else "",
                                 placeholder_text=placeholder,
                                 fg_color=P.surface0, text_color=P.text,
                                 border_width=0, height=32)
            entry.grid(row=row_idx, column=1, padx=(4, 12), pady=8, sticky="ew")

        self._api_type_var = ctk.StringVar(value="openai_compat")
        ctk.CTkLabel(frame, text="API Type", text_color=P.subtext,
                     font=ctk.CTkFont(size=12),
                     width=120, anchor="w").grid(row=3, column=0, padx=12, pady=8, sticky="w")
        ctk.CTkOptionMenu(frame, variable=self._api_type_var,
                          values=["openai_compat", "github_models"],
                          fg_color=P.surface0, button_color=P.blue,
                          button_hover_color=P.sky, text_color=P.text,
                          width=220).grid(row=3, column=1, padx=(4, 12), pady=8, sticky="w")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=(0, 14))
        ctk.CTkButton(btn_row, text="Cancel", width=100, height=32,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Save", width=100, height=32,
                      fg_color=P.blue, hover_color=P.sky,
                      text_color=P.base, command=self._save).pack(side="left", padx=6)

    def _save(self):
        name = self._field_vars["name"].get().strip()
        url  = self._field_vars["url"].get().strip()
        model = self._field_vars["model"].get().strip()
        if not name or not url:
            return
        self._on_save({
            "name":     name,
            "url":      url,
            "model":    model or "",
            "api_type": self._api_type_var.get(),
        })
        self.destroy()


# ── Main application ──────────────────────────────────────────────────────────

class App(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("MeshCore AI Bridge")
        self.geometry("1240x800")
        self.minsize(900, 600)

        self._runner: _BridgeRunner | None = None
        self._log_queue: queue.Queue       = queue.Queue()
        self._map_queue:  queue.Queue      = queue.Queue(maxsize=500)

        self._vars:      dict[str, tk.Variable] = {}
        self._feat_vars: dict[str, ctk.BooleanVar] = {}

        # Custom provider presets added by user: list[{name, url, model, api_type}]
        self._custom_providers: list[dict] = []

        # Per-provider last-used model: {provider_name: model_str}
        self._provider_models:   dict[str, str] = {}
        self._gate_models:       dict[str, str] = {}

        # Discovery map: {callsign: {lat, lon, name, snr, last_seen}}
        self._map_nodes: dict[str, dict] = {}

        # Animation state
        self._pulse_phase   = 0.0
        self._pulse_job: str | None = None

        self._build_ui()
        self._setup_logging()
        self._load_settings()
        self._map_nodes_load()
        self._poll_log()
        self._poll_map_queue()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        self._build_topbar()
        self._build_settings_panel()
        self._build_right_panel()
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
            command=self._on_tab_changed,
        )
        self._tab.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        for name in ("Connection", "Commands", "Timing", "Features", "Nodes"):
            self._tab.add(name)

        self._build_tab_connection(self._tab.tab("Connection"))
        self._build_tab_commands(self._tab.tab("Commands"))
        self._build_tab_timing(self._tab.tab("Timing"))
        self._build_tab_features(self._tab.tab("Features"))
        self._build_tab_map(self._tab.tab("Nodes"))

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

    # Provider → (url, model, api_key_hint)
    _PROVIDER_DEFAULTS: dict[str, tuple[str, str, str]] = {
        "openai_compat": (
            DEFAULT_OPENAI_COMPAT_URL,
            DEFAULT_CONFIG["model"],
            "Optional — leave blank for LM Studio and most local servers",
        ),
        "github_models": (
            DEFAULT_GITHUB_MODELS_URL,
            "openai/gpt-4.1",
            "Required — GitHub Personal Access Token with models:read scope",
        ),
    }

    def _apply_llm_preset(self, provider: str,
                          url_key: str = "lm_url",
                          model_key: str = "model",
                          provider_key: str = "llm_provider",
                          api_key_hint_widget: str = "_api_key_hint",
                          per_provider_store: str = "_provider_models"):
        provider_var = self._vars.get(provider_key)
        url_var      = self._vars.get(url_key)
        model_var    = self._vars.get(model_key)
        if not url_var or not model_var:
            return
        if provider_var:
            provider_var.set(provider)

        defaults = self._PROVIDER_DEFAULTS.get(provider)
        if defaults:
            url_default, model_default, key_hint = defaults
            url_var.set(url_default)
            # Restore the last model the user used with this provider
            store: dict = getattr(self, per_provider_store, {})
            stored_model = store.get(provider, "").strip()
            model_var.set(stored_model if stored_model else model_default)
            hint_widget = getattr(self, api_key_hint_widget, None)
            if hint_widget:
                hint_widget.configure(text=f"   {key_hint}")

    def _on_provider_changed(self, provider: str):
        # Save current model under the old provider before switching
        old = self._vars.get("llm_provider")  # already updated to new
        # old == provider now, so we track via model trace in _on_model_edited
        self._apply_llm_preset(
            provider,
            url_key="lm_url", model_key="model",
            provider_key="llm_provider",
            api_key_hint_widget="_api_key_hint",
            per_provider_store="_provider_models",
        )

    def _on_gate_provider_changed(self, provider: str):
        self._apply_llm_preset(
            provider,
            url_key="local_gate_url", model_key="local_gate_model",
            provider_key="local_gate_provider",
            api_key_hint_widget="_gate_api_key_hint",
            per_provider_store="_gate_models",
        )

    def _on_model_edited(self, *_args):
        """Keep _provider_models in sync whenever the model entry changes."""
        prov_var = self._vars.get("llm_provider")
        model_var = self._vars.get("model")
        if prov_var and model_var:
            self._provider_models[prov_var.get()] = model_var.get()

    def _on_gate_model_edited(self, *_args):
        prov_var = self._vars.get("local_gate_provider")
        model_var = self._vars.get("local_gate_model")
        if prov_var and model_var:
            self._gate_models[prov_var.get()] = model_var.get()

    _INTENSITY_HINTS: dict[str, str] = {
        "off":        "Auto-engage disabled. Bridge only responds to explicit !ai or !bot commands and @mentions.",
        "minimal":    "Fires only when message contains an AI keyword (ai / sztuczna / intelig) AND local LLM confirms it's worth replying. Most messages ignored.",
        "keyword":    "Keyword-driven: replies when message has '?', the word 'ai/bot/robot', or bot-address. No LLM call — instant and free.",
        "normal":     "Local LLM reads every message and decides YES/NO. Best balance. Uses local gate backend (configure in Connection tab).",
        "aggressive": "Replies to almost all non-trivial messages. No LLM call needed — just fires. Use with message cooldown to avoid spam.",
    }

    def _on_intensity_changed(self, value: str):
        self._update_intensity_hint(value)

    def _update_intensity_hint(self, value: str):
        hint = self._INTENSITY_HINTS.get(value, "")
        if hasattr(self, "_intensity_hint"):
            self._intensity_hint.configure(text=hint)

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

        self._sec(scroll, "🤖", "LLM Backend")

        self._vars["llm_provider"] = ctk.StringVar(value=DEFAULT_CONFIG["llm_provider"])
        row = self._row(scroll, "Provider")
        prov_inner = ctk.CTkFrame(row, fg_color="transparent")
        prov_inner.grid(row=0, column=1, sticky="w")
        self._provider_menu = ctk.CTkOptionMenu(
            prov_inner,
            variable=self._vars["llm_provider"],
            values=["openai_compat", "github_models"],
            command=self._on_provider_changed,
            fg_color=P.surface0,
            button_color=P.blue,
            button_hover_color=P.sky,
            text_color=P.text,
            width=200,
        )
        self._provider_menu.pack(side="left")
        ctk.CTkButton(prov_inner, text="+", width=30, height=28,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, corner_radius=6,
                      command=self._open_add_provider_dialog).pack(side="left", padx=(6, 0))
        self._hint(scroll, "openai_compat = LM Studio, Ollama, any OpenAI-compatible server")
        self._hint(scroll, "github_models = GitHub Models marketplace (requires PAT with models:read)")

        self._vars["lm_url"] = ctk.StringVar(value=DEFAULT_CONFIG["lm_url"])
        row = self._row(scroll, "API URL")
        ctk.CTkEntry(row, textvariable=self._vars["lm_url"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")

        self._vars["model"] = ctk.StringVar(value=DEFAULT_CONFIG["model"])
        self._vars["model"].trace_add("write", self._on_model_edited)
        row = self._row(scroll, "Model Name")
        ctk.CTkEntry(row, textvariable=self._vars["model"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")
        self._hint(scroll, "Switching provider restores the last model you used for that provider")

        # Keep vars for _collect_config but show only button
        self._vars["llm_api_key"] = ctk.StringVar(value="")
        self._vars["local_gate_api_key"] = ctk.StringVar(value="")
        row = self._row(scroll, "API Keys")
        ctk.CTkButton(row, text="🔑  Manage API Keys", height=30, width=170,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, corner_radius=6,
                      command=self._open_api_keys_dialog).grid(row=0, column=1, sticky="w")

        self._sec(scroll, "🧠", "Local Auto-Gate Backend")

        self._vars["local_gate_provider"] = ctk.StringVar(
            value=DEFAULT_CONFIG.get("local_gate_provider", "openai_compat")
        )
        row = self._row(scroll, "Gate Provider")
        gate_inner = ctk.CTkFrame(row, fg_color="transparent")
        gate_inner.grid(row=0, column=1, sticky="w")
        self._gate_provider_menu = ctk.CTkOptionMenu(
            gate_inner,
            variable=self._vars["local_gate_provider"],
            values=["openai_compat", "github_models"],
            command=self._on_gate_provider_changed,
            fg_color=P.surface0,
            button_color=P.blue,
            button_hover_color=P.sky,
            text_color=P.text,
            width=200,
        )
        self._gate_provider_menu.pack(side="left")

        self._vars["local_gate_url"] = ctk.StringVar(
            value=DEFAULT_CONFIG.get("local_gate_url", DEFAULT_OPENAI_COMPAT_URL)
        )
        row = self._row(scroll, "Gate URL")
        ctk.CTkEntry(row, textvariable=self._vars["local_gate_url"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")

        self._vars["local_gate_model"] = ctk.StringVar(
            value=DEFAULT_CONFIG.get("local_gate_model", DEFAULT_CONFIG["model"])
        )
        self._vars["local_gate_model"].trace_add("write", self._on_gate_model_edited)
        row = self._row(scroll, "Gate Model")
        ctk.CTkEntry(row, textvariable=self._vars["local_gate_model"],
                     fg_color=P.surface0, text_color=P.text,
                     border_width=0).grid(row=0, column=1, sticky="ew")
        self._hint(scroll, "Used only when local-gate toggle is enabled in Features → Auto Engage")
        self._hint(scroll, "Gate API key is managed via the 🔑 Manage API Keys button above")

        self._sec(scroll, "🔗", "News API (optional)")

        self._vars["news_api_key"] = ctk.StringVar(value="")
        row = self._row(scroll, "API Key")
        btn_inner = ctk.CTkFrame(row, fg_color="transparent")
        btn_inner.grid(row=0, column=1, sticky="w")
        ctk.CTkButton(btn_inner, text="🔑  Manage API Keys", height=28, width=170,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, corner_radius=6,
                      command=self._open_api_keys_dialog).pack(side="left")
        self._hint(scroll, "newsapi.org key + provider keys — all in one popup")

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
        ctk.CTkButton(row, text="📡  Manage Channels", height=30, width=180,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, corner_radius=6,
                      command=self._open_channels_dialog).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "fetch from device, pick which channels to listen on, add private channels")

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

        self._sec(scroll, "🪙", "Token Safety")

        self._vars["token_budget_total"] = ctk.IntVar(
            value=DEFAULT_CONFIG.get("token_budget_total", 0))
        row = self._row(scroll, "Session Total Budget")
        _CTkSpinbox(row, from_=0, to=500000, increment=100,
                    variable=self._vars["token_budget_total"],
                    width=160).grid(row=0, column=1, sticky="w")

        self._vars["token_budget_prompt"] = ctk.IntVar(
            value=DEFAULT_CONFIG.get("token_budget_prompt", 0))
        row = self._row(scroll, "Prompt Token Budget")
        _CTkSpinbox(row, from_=0, to=500000, increment=100,
                    variable=self._vars["token_budget_prompt"],
                    width=160).grid(row=0, column=1, sticky="w")

        self._vars["token_budget_completion"] = ctk.IntVar(
            value=DEFAULT_CONFIG.get("token_budget_completion", 0))
        row = self._row(scroll, "Completion Token Budget")
        _CTkSpinbox(row, from_=0, to=500000, increment=100,
                    variable=self._vars["token_budget_completion"],
                    width=160).grid(row=0, column=1, sticky="w")
        self._hint(scroll, "0 = unlimited. Bridge stops LLM calls when budget is exhausted")

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

        self._sec(scroll, "❓", "Unknown Commands")

        self._feat_vars["__reply_unknown__"] = ctk.BooleanVar(value=True)
        unk_card = ctk.CTkFrame(scroll, fg_color=P.surface0, corner_radius=8)
        unk_card.pack(fill="x", padx=4, pady=4)
        ctk.CTkCheckBox(
            unk_card,
            text="Reply to unknown command  (sends help hint)",
            variable=self._feat_vars["__reply_unknown__"],
            text_color=P.text,
            checkmark_color=P.base,
            fg_color=P.blue, hover_color=P.sky,
            border_color=P.surface2,
        ).pack(anchor="w", padx=12, pady=10)

        self._sec(scroll, "📣", "Mentions")

        self._feat_vars["__mention_ai__"] = ctk.BooleanVar(value=True)
        mention_card = ctk.CTkFrame(scroll, fg_color=P.surface0, corner_radius=8)
        mention_card.pack(fill="x", padx=4, pady=4)
        ctk.CTkCheckBox(
            mention_card,
            text="Reply to @[name] mention with AI",
            variable=self._feat_vars["__mention_ai__"],
            text_color=P.text,
            checkmark_color=P.base,
            fg_color=P.blue, hover_color=P.sky,
            border_color=P.surface2,
        ).pack(anchor="w", padx=12, pady=10)

        self._sec(scroll, "🧠", "Auto Engage")

        auto_card = ctk.CTkFrame(scroll, fg_color=P.surface0, corner_radius=8)
        auto_card.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(
            auto_card, text="Reply intensity",
            text_color=P.text, font=("Segoe UI", 13),
        ).pack(anchor="w", padx=12, pady=(10, 2))
        self._vars["auto_engage_intensity"] = ctk.StringVar(value="off")
        ctk.CTkOptionMenu(
            auto_card,
            variable=self._vars["auto_engage_intensity"],
            values=["off", "minimal", "keyword", "normal", "aggressive"],
            command=self._on_intensity_changed,
            fg_color=P.surface1,
            button_color=P.blue,
            button_hover_color=P.sky,
            text_color=P.text,
            width=160,
        ).pack(anchor="w", padx=12, pady=(0, 6))
        self._intensity_hint = ctk.CTkLabel(
            auto_card, text="",
            text_color=P.overlay0, font=ctk.CTkFont(size=10),
            wraplength=320, justify="left",
        )
        self._intensity_hint.pack(anchor="w", padx=12, pady=(0, 8))
        self._update_intensity_hint("off")

        self._feat_vars["__local_gate__"] = ctk.BooleanVar(
            value=bool(DEFAULT_CONFIG.get("local_gate_enabled", False))
        )
        ctk.CTkCheckBox(
            auto_card,
            text="Use local LLM only for auto-engage decision",
            variable=self._feat_vars["__local_gate__"],
            text_color=P.text,
            checkmark_color=P.base,
            fg_color=P.blue, hover_color=P.sky,
            border_color=P.surface2,
        ).pack(anchor="w", padx=12, pady=(0, 8))
        self._hint(auto_card, "When enabled: gate uses local_gate_* config, final replies still use main provider")

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

    # ── Tab: Map ──────────────────────────────────────────────────────────────

    def _build_tab_map(self, tab):
        """Sidebar Map tab: shows heard-node list sorted by last seen."""
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(top, text="📡  Heard Nodes",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=P.blue).pack(side="left")
        self._map_count_label = ctk.CTkLabel(top, text="0 nodes",
                                             font=ctk.CTkFont(size=11),
                                             text_color=P.overlay0)
        self._map_count_label.pack(side="right", padx=4)
        ctk.CTkButton(top, text="Clear", width=60, height=26,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.text, corner_radius=6,
                      command=self._map_clear).pack(side="right", padx=4)

        self._node_list_frame = ctk.CTkScrollableFrame(
            tab, fg_color=P.base, corner_radius=8)
        self._node_list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _map_clear(self):
        self._map_nodes.clear()
        self._node_list_update()
        # Remove all markers from the live map
        if _HAS_MAPVIEW and hasattr(self, "_map_markers"):
            for m in self._map_markers.values():
                try:
                    m.delete()
                except Exception:
                    pass
            self._map_markers.clear()
        self._map_nodes_save()

    def _map_redraw(self):
        """No-op: replaced by tkintermapview markers."""
        pass

    def _map_refresh_all_markers(self):
        """Place/update all GPS nodes as markers on the tkintermapview."""
        if not _HAS_MAPVIEW or not getattr(self, "_mapview_ready", False) or not hasattr(self, "_mapview"):
            return
        for cs, nd in self._map_nodes.items():
            self._map_place_marker(cs, nd)

    def _map_place_marker(self, callsign: str, nd: dict):
        """Add or update a single marker on the map for the given node."""
        if not _HAS_MAPVIEW or not getattr(self, "_mapview_ready", False) or not hasattr(self, "_mapview"):
            return
        lat = nd.get("lat")
        lon = nd.get("lon")
        if not lat or not lon or (abs(lat) < 0.001 and abs(lon) < 0.001):
            return
        node_type = nd.get("node_type", None)
        if node_type == 2:
            color = P.teal
        elif node_type == 1:
            color = P.mauve
        else:
            color = P.text
        snr = nd.get("snr", "")
        snr_str = f" {snr}dB" if snr != "" else ""
        label = f"{callsign}{snr_str}"
        # Build or reuse cached PIL icon for this color
        if _HAS_MAPVIEW and color not in _marker_icon_cache:
            _marker_icon_cache[color] = _make_marker_icon(color)
        icon = _marker_icon_cache.get(color)
        # Remove old marker if exists
        old = self._map_markers.get(callsign)
        if old is not None:
            try:
                old.delete()
            except Exception:
                pass
        try:
            marker = self._mapview.set_marker(
                lat, lon,
                text=label,
                icon=icon,
                icon_anchor="center",
                text_color="#111111",
                font=("Consolas", 10, "bold"),
            )
            self._map_markers[callsign] = marker
        except Exception:
            pass

    def _map_on_hover(self, event):
        """No-op: tkintermapview handles tooltips natively."""
        pass


    def _poll_map_queue(self):
        changed = False
        try:
            while True:
                item = self._map_queue.get_nowait()
                callsign = item.get("callsign", "?")
                kind = item.get("kind", "advert")
                if kind == "advert":
                    self._map_nodes[callsign] = item
                    changed = True
                else:
                    # contact-only: update last_seen, preserve position
                    existing = self._map_nodes.get(callsign, {})
                    existing["callsign"] = callsign
                    existing["last_seen"] = item.get("last_seen", time.time())
                    if item.get("snr", "") != "":
                        existing["snr"] = item["snr"]
                    if item.get("node_type") is not None:
                        existing["node_type"] = item["node_type"]
                    self._map_nodes[callsign] = existing
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self._node_list_update()
            self._map_nodes_save()
            # Update live map markers for all changed nodes with GPS
            if _HAS_MAPVIEW and getattr(self, "_mapview_ready", False) and hasattr(self, "_mapview"):
                for cs, nd in self._map_nodes.items():
                    if nd.get("lat") and nd.get("lon"):
                        self._map_place_marker(cs, nd)
        self.after(2000, self._poll_map_queue)

    def _node_list_update(self):
        """Rebuild the sidebar node list sorted by last_seen descending."""
        if not hasattr(self, "_node_list_frame"):
            return
        frame = self._node_list_frame
        for w in frame.winfo_children():
            w.destroy()
        now = time.time()
        sorted_nodes = sorted(
            self._map_nodes.items(),
            key=lambda kv: kv[1].get("last_seen", 0),
            reverse=True,
        )
        self._map_count_label.configure(
            text=f"{len(sorted_nodes)} node{'s' if len(sorted_nodes) != 1 else ''}"
        )
        for cs, nd in sorted_nodes:
            # Color row by node type: 2=repeater (teal), 1=companion (mauve+bold), else default
            node_type = nd.get("node_type", None)
            if node_type == 2:
                name_color = P.teal
                type_icon  = "🔁 "
                name_font  = ctk.CTkFont(size=12, weight="bold")
            elif node_type == 1:
                name_color = P.mauve
                type_icon  = "📱 "
                name_font  = ctk.CTkFont(size=12, weight="bold", slant="italic")
            else:
                name_color = P.text
                type_icon  = ""
                name_font  = ctk.CTkFont(size=12, weight="bold")
            row = ctk.CTkFrame(frame, fg_color=P.surface0, corner_radius=6)
            row.pack(fill="x", padx=4, pady=2)
            row.columnconfigure(1, weight=1)
            # Click: zoom to node on map
            _cs = cs  # capture for closure
            _bind = lambda _e, c=_cs: self._map_zoom_to(c)
            row.bind("<Button-1>", _bind)
            # Hover: highlight row
            def _enter(e, r=row): r.configure(fg_color=P.surface1)
            def _leave(e, r=row):
                under = r.winfo_containing(e.x_root, e.y_root)
                try:
                    if under and str(under).startswith(str(r)):
                        return
                except Exception:
                    pass
                r.configure(fg_color=P.surface0)
            row.bind("<Enter>", _enter)
            row.bind("<Leave>", _leave)
            age_s = int(now - nd.get("last_seen", now))
            if age_s < 60:
                age_str = f"{age_s}s ago"
            elif age_s < 3600:
                age_str = f"{age_s // 60}m ago"
            else:
                age_str = f"{age_s // 3600}h ago"
            lat = nd.get("lat")
            lon = nd.get("lon")
            pos_str = (f"{lat:.4f}, {lon:.4f}"
                       if (lat and lon and (abs(lat) > 0.001 or abs(lon) > 0.001))
                       else "no GPS")
            snr = nd.get("snr", "")
            snr_str = f" · {snr}dB" if snr != "" else ""
            lbl_name = ctk.CTkLabel(row, text=f"{type_icon}{cs}",
                         font=name_font,
                         text_color=name_color, anchor="w")
            lbl_name.grid(row=0, column=0, padx=(8, 4), pady=4, sticky="w")
            lbl_pos = ctk.CTkLabel(row,
                         text=f"{pos_str}{snr_str}",
                         font=ctk.CTkFont(size=10),
                         text_color=P.overlay1, anchor="w")
            lbl_pos.grid(row=1, column=0, padx=(8, 4), pady=(0, 4), sticky="w")
            lbl_age = ctk.CTkLabel(row, text=age_str,
                         font=ctk.CTkFont(size=10),
                         text_color=P.overlay0, anchor="e")
            lbl_age.grid(row=0, column=1, padx=(4, 8), pady=4, sticky="e", rowspan=2)
            # Bind all child widgets so click + hover anywhere on row works
            for widget in (lbl_name, lbl_pos, lbl_age):
                widget.bind("<Button-1>", _bind)
                widget.bind("<Enter>", _enter)
                widget.bind("<Leave>", _leave)

    def _map_zoom_to(self, callsign: str):
        """Switch to Map tab and zoom to the node's position."""
        self._right_tabs.set("Map")
        self._init_mapview_lazy()
        nd = self._map_nodes.get(callsign, {})
        lat, lon = nd.get("lat"), nd.get("lon")
        if lat and lon and (abs(lat) > 0.001 or abs(lon) > 0.001):
            self.after(200, lambda: (
                self._mapview.set_position(lat, lon),
                self._mapview.set_zoom(13),
            ))

    def _map_nodes_save(self):
        """Persist current discoveries to JSON."""
        try:
            data: dict = {}
            for cs, nd in self._map_nodes.items():
                data[cs] = {k: v for k, v in nd.items() if k != "kind"}
            with open(_DISCOVERIES_FILE, "w", encoding="utf-8") as fh:
                json.dump({"nodes": data, "saved": time.time()}, fh, indent=2)
        except Exception as exc:
            log.debug("discoveries save failed: %s", exc)

    def _map_nodes_load(self):
        """Restore discoveries from JSON on startup."""
        try:
            with open(_DISCOVERIES_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            for cs, nd in data.get("nodes", {}).items():
                self._map_nodes[cs] = nd
            if self._map_nodes:
                self._node_list_update()
                # Schedule marker placement after UI is ready
                self.after(500, self._map_refresh_all_markers)
                log.info("Loaded %d discoveries from %s",
                         len(self._map_nodes), _DISCOVERIES_FILE)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("discoveries load failed: %s", exc)

    # ── Dialog helpers ────────────────────────────────────────────────────────

    def _open_channels_dialog(self):
        _ChannelsDialog(self, self._runner, self._vars["listen_channels"])

    def _open_api_keys_dialog(self):
        _APIKeysDialog(self, self._vars)

    def _open_add_provider_dialog(self):
        _AddProviderDialog(self, self._on_custom_provider_saved)

    def _on_custom_provider_saved(self, preset: dict):
        """Called by _AddProviderDialog when user saves a new provider."""
        self._custom_providers.append(preset)
        # Add to both provider menus
        name = preset["name"]
        for menu_attr in ("_provider_menu", "_gate_provider_menu"):
            menu = getattr(self, menu_attr, None)
            if menu:
                current = list(menu.cget("values")) if hasattr(menu, "cget") else []
                if name not in current:
                    menu.configure(values=current + [name])
        # Store preset so _apply_llm_preset can resolve it
        self._PROVIDER_DEFAULTS[name] = (preset["url"], preset["model"],
                                          "Custom provider")

    # ── Console panel ─────────────────────────────────────────────────────────

    def _build_right_panel(self):
        """Right-area panel with Console and Map tabs."""
        right = ctk.CTkFrame(self, fg_color=P.crust, corner_radius=0)
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._right_tabs = ctk.CTkTabview(
            right,
            fg_color=P.crust,
            segmented_button_fg_color=P.mantle,
            segmented_button_selected_color=P.blue,
            segmented_button_selected_hover_color=P.sky,
            segmented_button_unselected_color=P.mantle,
            segmented_button_unselected_hover_color=P.surface1,
            text_color=P.text,
            text_color_disabled=P.overlay0,
            command=self._on_right_tab_changed,
        )
        self._right_tabs.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self._right_tabs.add("Console")
        self._right_tabs.add("Map")

        self._build_console_tab(self._right_tabs.tab("Console"))
        self._build_map_tab(self._right_tabs.tab("Map"))

    def _build_console_tab(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # header bar with Clear button
        hdr = ctk.CTkFrame(tab, fg_color=P.mantle, corner_radius=0, height=36)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(0, weight=1)

        # Restart-required banner (hidden by default)
        self._restart_bar = ctk.CTkFrame(tab, fg_color=P.yellow,
                                          corner_radius=0, height=28)
        self._restart_bar.grid(row=0, column=0, sticky="ew")
        self._restart_bar.grid_propagate(False)
        self._restart_bar.grid_remove()
        ctk.CTkLabel(self._restart_bar,
                     text="⚠  Settings changed — restart bridge to apply",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=P.base).pack(side="left", padx=10, pady=4)

        if sys.platform == "win32":
            mono = ("Cascadia Code", 10)
        elif sys.platform == "darwin":
            mono = ("Menlo", 10)
        else:
            mono = ("DejaVu Sans Mono", 10)

        self._console = ctk.CTkTextbox(
            tab,
            fg_color=P.crust, text_color=P.text,
            font=mono, wrap="word", state="disabled",
            scrollbar_button_color=P.surface1,
            scrollbar_button_hover_color=P.surface2,
        )
        self._console.grid(row=1, column=0, sticky="nsew")

        ctk.CTkButton(hdr, text="Clear", width=60, height=24,
                      fg_color=P.surface1, hover_color=P.surface2,
                      text_color=P.subtext, corner_radius=6,
                      font=ctk.CTkFont(size=11),
                      command=self._clear_console).grid(row=0, column=1,
                                                         padx=8, pady=6,
                                                         sticky="e")

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

    def _build_map_tab(self, tab):
        """Map tab placeholder — mapview is created lazily on first open."""
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        self._map_markers: dict = {}  # callsign -> TkinterMapView marker
        self._map_tab_frame = tab      # stored for lazy init
        self._mapview_ready = False

        if not _HAS_MAPVIEW:
            ctk.CTkLabel(tab,
                         text="tkintermapview not installed.\nRun: pip install tkintermapview",
                         text_color=P.overlay1, justify="center").grid(
                row=0, column=0)
            return

        # Placeholder shown before first open
        self._map_placeholder = ctk.CTkLabel(
            tab, text="Open this tab to load the map",
            text_color=P.overlay0, font=ctk.CTkFont(size=12))
        self._map_placeholder.grid(row=0, column=0)

    def _init_mapview_lazy(self):
        """Create TkinterMapView on first Map tab open (saves network on console-only use)."""
        if self._mapview_ready or not _HAS_MAPVIEW:
            return
        self._mapview_ready = True
        if hasattr(self, "_map_placeholder"):
            self._map_placeholder.grid_remove()
        tab = self._map_tab_frame
        self._mapview = tkintermapview.TkinterMapView(
            tab, corner_radius=0, bg_color=P.crust,
            database_path=_TILE_CACHE_FILE,  # cache tiles locally for faster reloads
        )
        self._mapview.grid(row=0, column=0, sticky="nsew")
        self._mapview.set_position(50.0, 19.0)
        self._mapview.set_zoom(6)
        # Place existing nodes after widget settles
        self.after(300, self._map_refresh_all_markers)

    def _on_right_tab_changed(self):
        """Called when Console/Map tab switches; lazy-init + refresh markers."""
        if self._right_tabs.get() == "Map" and _HAS_MAPVIEW:
            self._init_mapview_lazy()
            if self._mapview_ready:
                self.after(150, self._map_refresh_all_markers)

    def _on_tab_changed(self, name: str):
        """Sidebar tab changed — nothing to toggle in right area anymore."""
        pass

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

        config = load_persisted_config()

        config["serial_port"]          = _get("serial_port")  or DEFAULT_CONFIG["serial_port"]
        config["baud_rate"]            = int(_get("baud_rate", DEFAULT_CONFIG["baud_rate"]))
        config["llm_provider"]         = _get("llm_provider") or DEFAULT_CONFIG["llm_provider"]
        config["lm_url"]               = _get("lm_url")       or DEFAULT_CONFIG["lm_url"]
        config["model"]                = _get("model")        or DEFAULT_CONFIG["model"]
        config["llm_api_key"]          = _get("llm_api_key") or None
        config["local_gate_provider"]  = _get("local_gate_provider") or DEFAULT_CONFIG.get("local_gate_provider", "openai_compat")
        config["local_gate_url"]       = _get("local_gate_url") or DEFAULT_CONFIG.get("local_gate_url", DEFAULT_OPENAI_COMPAT_URL)
        config["local_gate_model"]     = _get("local_gate_model") or DEFAULT_CONFIG.get("local_gate_model", DEFAULT_CONFIG["model"])
        config["local_gate_api_key"]   = _get("local_gate_api_key") or None
        ai_prefix_val = _get("ai_prefix", DEFAULT_CONFIG["ai_prefix"])
        bot_prefix_val = _get("bot_prefix", DEFAULT_CONFIG["bot_prefix"])
        config["ai_prefix"]            = ai_prefix_val if ai_prefix_val is not None else DEFAULT_CONFIG["ai_prefix"]
        config["bot_prefix"]           = bot_prefix_val if bot_prefix_val is not None else DEFAULT_CONFIG["bot_prefix"]
        config["message_cooldown_s"]   = float(_get("message_cooldown_s", 0))
        config["reply_delay_s"]        = float(_get("reply_delay_s",
                                                    DEFAULT_CONFIG["reply_delay_s"]))
        config["telemetry_interval_s"] = int(float(_get(
            "telemetry_interval_s", DEFAULT_CONFIG["telemetry_interval_s"])))
        config["monitor_reminder_s"]   = int(float(_get(
            "monitor_reminder_s", DEFAULT_CONFIG["monitor_reminder_s"])))
        config["channel_context_msgs"] = int(float(_get(
            "channel_context_msgs", DEFAULT_CONFIG["channel_context_msgs"])))
        config["token_budget_total"] = max(0, int(float(_get(
            "token_budget_total", DEFAULT_CONFIG.get("token_budget_total", 0)))))
        config["token_budget_prompt"] = max(0, int(float(_get(
            "token_budget_prompt", DEFAULT_CONFIG.get("token_budget_prompt", 0)))))
        config["token_budget_completion"] = max(0, int(float(_get(
            "token_budget_completion", DEFAULT_CONFIG.get("token_budget_completion", 0)))))
        config["news_api_key"]         = _get("news_api_key") or None

        listen_raw = (_get("listen_channels") or "").strip()
        if listen_raw.lower() in {"none", "null", "[]"}:
            listen_raw = ""
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
        config["reply_unknown_command"] = self._feat_vars.get(
            "__reply_unknown__", ctk.BooleanVar(value=True)).get()
        config["mention_ai_enabled"] = self._feat_vars.get(
            "__mention_ai__", ctk.BooleanVar(value=True)).get()
        config["auto_engage_intensity"] = self._vars.get(
            "auto_engage_intensity", ctk.StringVar(value="off")).get()
        config["auto_engage_worth_reply"] = config["auto_engage_intensity"] != "off"
        config["local_gate_enabled"] = self._feat_vars.get(
            "__local_gate__", ctk.BooleanVar(value=False)).get()
        disabled: set[str] = set()
        for key, _ in BOT_FEATURES:
            if not self._feat_vars.get(key, ctk.BooleanVar(value=True)).get():
                disabled.add(key)
        config["disabled_commands"] = disabled

        return config

    def _save_settings(self):
        data = self._collect_config()
        try:
            save_user_config(data)
        except Exception as exc:
            log.debug("Could not save settings: %s", exc)

    def _load_settings(self):
        try:
            data = load_persisted_config()
        except Exception as exc:
            log.debug("Could not load settings: %s", exc)
            return

        for key, var in self._vars.items():
            if key in data:
                try:
                    if key == "listen_channels":
                        val = data[key]
                        if val in (None, "", "None", "null", []):
                            var.set("")
                            continue
                        if isinstance(val, (list, tuple, set)):
                            var.set(" ".join(str(x) for x in val))
                            continue
                    if key == "reply_channel" and data[key] in (None, "None", "null", ""):
                        var.set("")
                        continue
                    var.set(data[key])
                except Exception:
                    pass

        # Feature toggles stored as real config keys.
        feat_map = {
            "__ai__": "ai_enabled",
            "__reply_unknown__": "reply_unknown_command",
            "__mention_ai__": "mention_ai_enabled",
            "__local_gate__": "local_gate_enabled",
        }
        for feat_key, cfg_key in feat_map.items():
            var = self._feat_vars.get(feat_key)
            if var is None or cfg_key not in data:
                continue
            try:
                var.set(bool(data[cfg_key]))
            except Exception:
                pass

        disabled = set(data.get("disabled_commands") or [])
        for key, _ in BOT_FEATURES:
            var = self._feat_vars.get(key)
            if var is None:
                continue
            try:
                var.set(key not in disabled)
            except Exception:
                pass

        # Refresh intensity hint after load
        intensity_var = self._vars.get("auto_engage_intensity")
        if intensity_var:
            self._update_intensity_hint(intensity_var.get())

        # Seed per-provider model memory from loaded config
        prov = data.get("llm_provider") or DEFAULT_CONFIG["llm_provider"]
        model = data.get("model") or DEFAULT_CONFIG["model"]
        if prov and model:
            self._provider_models[prov] = model
        gate_prov = data.get("local_gate_provider") or DEFAULT_CONFIG.get("local_gate_provider", "openai_compat")
        gate_model = data.get("local_gate_model") or DEFAULT_CONFIG.get("local_gate_model", DEFAULT_CONFIG["model"])
        if gate_prov and gate_model:
            self._gate_models[gate_prov] = gate_model

        self._bind_change_traces()

    def _on_setting_changed(self, *_args):
        """Show restart banner when settings change while bridge is running."""
        if (hasattr(self, "_restart_bar")
                and self._runner and self._runner.is_running):
            self._restart_bar.grid()

    def _bind_change_traces(self):
        """Bind write traces to all config vars so we can detect live changes."""
        for var in self._vars.values():
            try:
                var.trace_add("write", self._on_setting_changed)
            except Exception:
                pass
        for var in self._feat_vars.values():
            try:
                var.trace_add("write", self._on_setting_changed)
            except Exception:
                pass

    # ── Status & animation ────────────────────────────────────────────────────

    def _set_running(self, running: bool, stopping: bool = False):
        # Always hide restart bar when bridge state changes
        if hasattr(self, "_restart_bar"):
            self._restart_bar.grid_remove()
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
        self._runner = _BridgeRunner(config, self._map_queue)
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
        self._map_nodes_save()
        self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler("AIbridge.log", encoding="utf-8")],
    )
    # Suppress noisy third-party tile/image loggers
    for _noisy in ("tkintermapview", "PIL", "urllib3", "requests"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
