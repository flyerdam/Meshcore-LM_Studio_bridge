"""
Robust serial connection manager for MeshCore.

Handles automatic reconnection with exponential backoff when the companion
device resets or the COM port becomes blocked/unavailable.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from meshcore import MeshCore

log = logging.getLogger(__name__)


class SerialConnection:
    """
    Wraps MeshCore serial creation with:
    - Automatic reconnection on failure (exponential backoff).
    - Proper cleanup before reconnecting to avoid blocked COM ports.
    - DTR/RTS line control to prevent companion resets on connect.
    - Health-check capability so callers can detect a dead link.
    """

    def __init__(self, port: str, baud: int, cfg: dict):
        self.port = port
        self.baud = baud
        self._cfg = cfg

        self._mc: MeshCore | None = None
        self._connected = False
        self._reconnecting = False
        self._connect_lock = asyncio.Lock()
        self._connected_since: float | None = None
        self._last_command: str | None = None
        self._command_count = 0

        # Backoff parameters
        self._initial_delay = cfg.get("reconnect_delay_s", 5)
        self._max_delay = cfg.get("reconnect_max_delay_s", 60)
        self._max_retries = cfg.get("reconnect_max_retries", 0)  # 0 = infinite

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def mc(self) -> MeshCore | None:
        """The underlying MeshCore instance (may be *None* while reconnecting)."""
        return self._mc

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> MeshCore:
        """
        Establish the initial connection.  Raises on failure (no retry on
        first connect so the user sees the error immediately).
        """
        async with self._connect_lock:
            self._mc = await self._open_serial()
            self._connected = True
            self._connected_since = time.monotonic()
            self._last_command = None
            self._command_count = 0
            log.info("Serial connected on %s @ %d baud", self.port, self.baud)
            return self._mc

    async def disconnect(self):
        """Cleanly close the connection."""
        async with self._connect_lock:
            await self._close(reason="manual_disconnect")

    async def reconnect(self) -> MeshCore | None:
        """
        Attempt to re-establish the serial link with exponential backoff.
        Returns the new MeshCore instance, or *None* if max retries exhausted.
        """
        if self._reconnecting:
            # Another task is already handling reconnection
            while self._reconnecting:
                await asyncio.sleep(1)
            return self._mc

        async with self._connect_lock:
            self._reconnecting = True
            try:
                return await self._reconnect_loop()
            finally:
                self._reconnecting = False

    async def ensure_connected(self) -> MeshCore:
        """
        Return the current MeshCore instance, or trigger a reconnect if the
        link is down.  Useful as a guard before every serial operation.
        """
        if self._connected and self._mc is not None:
            return self._mc
        mc = await self.reconnect()
        if mc is None:
            raise ConnectionError("Unable to reconnect to MeshCore")
        return mc

    async def execute(self, coro_factory, command_name: str = "unknown_command"):
        """
        Execute an async MeshCore command, reconnecting on serial errors.

        *coro_factory* is a callable that, given a MeshCore instance, returns
        the coroutine to run.  Example::

            result = await serial_conn.execute(
                lambda mc: mc.commands.get_msg(timeout=0.5)
            )
        """
        try:
            mc = await self.ensure_connected()
            self._last_command = command_name
            self._command_count += 1
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            log.info("TX %s ts=%s", command_name, ts)
            result = await coro_factory(mc)
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            log.info("RX %s ts=%s result=%s", command_name, ts, type(result).__name__)
            return result
        except (OSError, ConnectionError, SerialConnectionError) as exc:
            log.warning("Serial error during command: %s – triggering reconnect", exc)
            self._connected = False
            self._log_disconnect(reason=f"command_error:{command_name}", exc=exc)
            mc = await self.reconnect()
            if mc is None:
                raise
            self._last_command = command_name
            self._command_count += 1
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            log.info("TX %s ts=%s", command_name, ts)
            result = await coro_factory(mc)
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            log.info("RX %s ts=%s result=%s", command_name, ts, type(result).__name__)
            return result

    # ── Internals ───────────────────────────────────────────────────────────

    async def _open_serial(self) -> MeshCore:
        """Create a fresh MeshCore serial connection."""
        log.info("Opening serial port %s @ %d …", self.port, self.baud)

        # Brief pause to let the OS release the port if it was recently closed.
        await asyncio.sleep(0.5)

        mc = await MeshCore.create_serial(self.port, self.baud)

        # Try to prevent companion reset by keeping DTR/RTS stable.
        # MeshCore wraps a serial.Serial – access it if possible.
        try:
            transport = getattr(mc, "_transport", None) or getattr(mc, "transport", None)
            serial_obj = getattr(transport, "serial", None) if transport else None
            if serial_obj is None:
                serial_obj = getattr(mc, "_serial", None) or getattr(mc, "serial", None)
            if serial_obj is not None:
                log.info(
                    "Serial lines on open %s: dtr=%s rts=%s dsr=%s cts=%s",
                    self.port,
                    getattr(serial_obj, "dtr", None),
                    getattr(serial_obj, "rts", None),
                    getattr(serial_obj, "dsr", None),
                    getattr(serial_obj, "cts", None),
                )
                serial_obj.dtr = False
                serial_obj.rts = False
                log.info(
                    "Serial lines after forcing low %s: dtr=%s rts=%s dsr=%s cts=%s",
                    self.port,
                    getattr(serial_obj, "dtr", None),
                    getattr(serial_obj, "rts", None),
                    getattr(serial_obj, "dsr", None),
                    getattr(serial_obj, "cts", None),
                )
        except Exception as exc:
            log.debug("Could not set DTR/RTS: %s (non-fatal)", exc)

        return mc

    async def _close(self, reason: str = "disconnect"):
        """Disconnect the current MeshCore instance gracefully."""
        self._log_disconnect(reason=reason)
        self._connected = False
        if self._mc is not None:
            try:
                await self._mc.disconnect()
                log.info("Serial disconnected from %s", self.port)
            except Exception as exc:
                log.debug("Error during serial disconnect: %s", exc)
            finally:
                self._mc = None
        self._connected_since = None

        # Allow OS time to fully release the port.
        await asyncio.sleep(1.0)

    async def _reconnect_loop(self) -> MeshCore | None:
        """Core reconnection loop with exponential backoff."""
        await self._close(reason="reconnect_start")

        delay = self._initial_delay
        attempt = 0

        while True:
            attempt += 1
            if 0 < self._max_retries < attempt:
                log.error(
                    "Max reconnection attempts (%d) reached – giving up",
                    self._max_retries,
                )
                return None

            log.warning(
                "Reconnect attempt %d – waiting %.1fs before retry on %s …",
                attempt, delay, self.port,
            )
            await asyncio.sleep(delay)

            try:
                self._mc = await self._open_serial()
                self._connected = True
                self._connected_since = time.monotonic()
                self._last_command = None
                self._command_count = 0
                log.info(
                    "Reconnected to %s on attempt %d", self.port, attempt
                )
                return self._mc
            except Exception as exc:
                log.error("Reconnect attempt %d failed: %s", attempt, exc)
                # Exponential backoff (capped)
                delay = min(delay * 2, self._max_delay)

    def _log_disconnect(self, reason: str, exc: Exception | None = None):
        if self._connected_since is None:
            return
        connected_for_s = time.monotonic() - self._connected_since
        if self._command_count == 0 and connected_for_s < 1.5:
            phase = "immediate_after_open"
        elif self._command_count <= 1:
            phase = "after_first_command"
        elif connected_for_s >= 3.0:
            phase = "after_several_seconds"
        else:
            phase = "unspecified"
        log.warning(
            "Serial disconnect phase=%s reason=%s connected_for=%.2fs commands=%d last_command=%s error=%s",
            phase,
            reason,
            connected_for_s,
            self._command_count,
            self._last_command or "none",
            exc,
        )


class SerialConnectionError(Exception):
    """Raised when a serial operation fails in a way that needs reconnection."""
