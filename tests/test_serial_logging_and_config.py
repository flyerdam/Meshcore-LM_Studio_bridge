import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


meshcore_stub = types.ModuleType("meshcore")
meshcore_stub.EventType = types.SimpleNamespace(ERROR="ERROR")
meshcore_stub.MeshCore = type("MeshCore", (), {"create_serial": AsyncMock()})
sys.modules.setdefault("meshcore", meshcore_stub)

from meshcore_bridge.config import build_config, parse_args
from meshcore_bridge.serial_connection import SerialConnection


class TestConfig(unittest.TestCase):
    def test_parse_args_supports_log_level(self):
        args = parse_args(["--log-level", "DEBUG"])
        cfg = build_config(args)
        self.assertEqual(cfg["log_level"], "DEBUG")


class FakeSerial:
    def __init__(self):
        self.dtr = True
        self.rts = True
        self.dsr = False
        self.cts = True


class TestSerialConnection(unittest.IsolatedAsyncioTestCase):
    async def test_open_serial_sets_dtr_rts_false(self):
        fake_serial = FakeSerial()
        fake_mc = types.SimpleNamespace(
            transport=types.SimpleNamespace(serial=fake_serial),
            disconnect=AsyncMock(),
        )
        with patch(
            "meshcore_bridge.serial_connection.MeshCore.create_serial",
            new=AsyncMock(return_value=fake_mc),
        ):
            conn = SerialConnection("COM3", 115200, {})
            await conn._open_serial()

        self.assertFalse(fake_serial.dtr)
        self.assertFalse(fake_serial.rts)

    async def test_execute_logs_tx_rx(self):
        conn = SerialConnection("COM3", 115200, {})
        conn._connected = True
        conn._connected_since = 0.0
        conn._mc = object()

        async def ok(_):
            return "ok"

        with patch("meshcore_bridge.serial_connection.time.monotonic", return_value=4.0):
            with self.assertLogs("meshcore_bridge.serial_connection", level="INFO") as logs:
                result = await conn.execute(ok, command_name="get_msg")

        self.assertEqual(result, "ok")
        all_logs = "\n".join(logs.output)
        self.assertIn("TX get_msg", all_logs)
        self.assertIn("RX get_msg", all_logs)


if __name__ == "__main__":
    unittest.main()
