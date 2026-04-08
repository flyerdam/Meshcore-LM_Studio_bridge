# Meshcore-LM_Studio_bridge
⚠️ Work in Progress: This project is currently in active development. Not all features work as intended, and you may encounter bugs with certain commands. Future plans include building a graphical user interface (GUI) to replace the command-line experience.

A Python-based companion service that bridges a LoRa mesh network (via MeshCore over USB/Serial) with a local Large Language Model (via an OpenAI-compatible API like LM Studio).

This tool allows users on a mesh network to interact with AI, fetch real-time internet data (weather, news, search), and perform network diagnostics directly over RF. Long AI responses are automatically chunked to respect LoRa packet limits, and the AI is injected with recent channel context so it can follow ongoing conversations.
Quick Start

Prerequisites: Python 3.10+, a LoRa radio connected via USB/Serial, and a local LLM server.

    Install Dependencies:
    Bash

    pip install meshcore requests

    Start your AI Model:
    Open LM Studio (or your preferred backend), load a model, and start the local server. Ensure it is running on http://localhost:1234/v1/chat/completions.

    Run the Bridge:
    Point the script to your radio's COM port (default is COM3).
    Bash

    python AIbridge.py --port COM3

Common Configuration Flags

You can customize the bot without editing the code:

    --port COM4: Set the serial port.

    --baud 115200: Set the baud rate.

    --model "model-name": Model name passed to the LLM backend.

    --news-key YOUR_KEY: Enable NewsAPI features.

    --listen-channels 0 2: Restrict listening to specific channels.

    --ai-prefix "!ai" / --bot-prefix "!b": Customize the trigger prefixes.

Available Mesh Commands

Network users can send the following commands over the mesh network.

AI Chat (!ai)

    !ai <question>: Query the local LLM. (Automatically chunked into 130-byte segments).

    !ai reset: Clears your specific conversation history from the AI's memory.

Web Tools (!b)

    !b weather <city>: Fetches current weather via Open-Meteo.

    !b search <query>: Instant answer summary from DuckDuckGo.

    !b news [topic]: Top headlines (requires --news-key).

Network Diagnostics (!b)

    !b ping / !b test: Returns a pong/ack with current SNR and hop count.

    !b info / !b stats: Displays node firmware, uptime, battery, noise floor, and packet stats.

    !b path / !b snr / !b channel: AI-driven and raw evaluations of signal quality and routing.

    !b monitor on/off: Toggles passive monitoring (alerts the channel if someone's signal is failing).

    !b reset: Resets paths to all known contacts, reverting to flood routing.

Acknowledgments

    MeshCore: Powered by the Python meshcore library.

    AI Assistance: Parts of this codebase and project structure were developed with the assistance of Anthropic's Claude.
