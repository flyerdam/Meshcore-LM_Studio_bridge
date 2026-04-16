"""
MeshCore ↔ LM Studio Bridge
============================
A modular bridge connecting a LoRa mesh network (MeshCore over USB/Serial)
with a local Large Language Model (OpenAI-compatible API).
"""

from meshcore_bridge.bridge import MeshCoreLLMBridge
from meshcore_bridge.config import DEFAULT_CONFIG, parse_args

__all__ = ["MeshCoreLLMBridge", "DEFAULT_CONFIG", "parse_args"]
