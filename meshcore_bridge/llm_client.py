"""
LM Studio (OpenAI-compatible) client for querying a local LLM.
"""

import logging
import re
from collections import deque

import requests

from meshcore_bridge.helpers import strip_think_tags, strip_tool_artifacts

log = logging.getLogger(__name__)


class LMStudioClient:

    def __init__(self, url: str, model: str, system_prompt: str, history_len: int = 5):
        self.url           = url
        self.model         = model
        self.system_prompt = system_prompt
        self.history_len   = history_len
        self._histories: dict[str, deque] = {}

    def _history(self, sender: str) -> deque:
        if sender not in self._histories:
            self._histories[sender] = deque(maxlen=self.history_len * 2)
        return self._histories[sender]

    def ask(self, sender: str, question: str,
            channel_context: list[dict] | None = None,
            save_history: bool = True) -> str:
        return self._call(sender, question,
                          save_history=save_history, channel_context=channel_context)

    def analyze(self, prompt: str) -> str:
        return self._call("__analysis__", prompt, save_history=False)

    def should_reply(self, sender: str, message: str,
                     channel_context: list[dict] | None = None,
                     mention_aliases: list[str] | None = None,
                     intensity: str = "normal") -> bool:
        """Tiered proactive-reply gate.

        cautious  – name/bot mentions only
        normal    – + any question mark + AI keywords
        aggressive – + LLM YES/NO gate for everything else
        """
        _ = sender

        text = (message or "").strip()
        if not text or intensity == "off":
            return False

        lower = text.lower()

        # Always: explicit !ai command.
        if lower.startswith("!ai"):
            return True

        norm = "".join(ch for ch in lower if ch.isalnum())
        aliases = [a for a in (mention_aliases or []) if a]

        # cautious+: direct name/bot-address.
        if any(a in norm for a in aliases):
            return True
        if re.search(r"\b(bocie|asystencie|assistant|asystent)\b", lower):
            return True

        if intensity == "cautious":
            return False

        # normal+: any question OR AI-related keyword.
        if "?" in lower:
            return True
        if re.search(r"\b(ai|sztuczna|intelig|robot|bot)\b", lower):
            return True

        if intensity == "normal":
            return False

        # aggressive: LLM gate.
        return self._gate_via_llm(text, channel_context)

    def _gate_via_llm(self, message: str,
                      channel_context: list[dict] | None = None) -> bool:
        """Lightweight LLM gate: returns True if model says YES."""
        ctx_lines = ""
        if channel_context:
            ctx_lines = "\n".join(
                f"{m['sender']}: {m['text']}" for m in channel_context[-5:]
            )
            ctx_lines = f"Recent channel messages:\n{ctx_lines}\n\n"

        prompt = (
            f"{ctx_lines}"
            f"New message: {message}\n\n"
            "Should an AI assistant on a radio channel reply to this message?\n"
            "Answer YES or NO only."
        )
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a gatekeeper for an AI radio assistant. "
                                "Decide if the new message deserves a reply. "
                                "Reply YES if it's a question, a request, or directly "
                                "addresses an AI. Reply NO for unrelated chat, "
                                "messages addressed to other people, or noise. "
                                "Output ONLY the word YES or NO."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 20,
                    "temperature": 0,
                    "stream": False,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return False
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if not content:
                return False
            return content.upper().startswith("YES")
        except Exception:
            return False

    def _call(self, sender: str, question: str,
              save_history: bool = True,
              channel_context: list[dict] | None = None) -> str:
        hist = self._history(sender)

        messages = [{"role": "system", "content": self.system_prompt}]

        # Inject channel context as "user" messages before the conversation history
        # This allows AI to "see" recent channel messages
        if channel_context:
            ctx_lines = "\n".join(
                f"{m['sender']}: {m['text']}" for m in channel_context
            )
            messages.append({
                "role": "user",
                "content": (
                    f"[Channel context – recent messages before question]\n"
                    f"{ctx_lines}"
                )
            })
            messages.append({
                "role": "assistant",
                "content": "I understand the channel context. Awaiting question."
            })

        if save_history:
            hist.append({"role": "user", "content": question})
            messages.extend(list(hist))
        else:
            messages.append({"role": "user", "content": question})
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model, "messages": messages,
                    "max_tokens": 300, "temperature": 0.7, "stream": False,
                },
                timeout=60,
            )
            if resp.status_code != 200:
                log.error("LM Studio HTTP %d: %s", resp.status_code, resp.text[:200])
                return f"[HTTP Error {resp.status_code}]"
            content = resp.json()["choices"][0]["message"]["content"].strip()
            content = strip_think_tags(content)
            content = strip_tool_artifacts(content)
            if save_history:
                hist.append({"role": "assistant", "content": content})
            return content
        except requests.exceptions.ConnectionError:
            return "[LM Studio unavailable]"
        except requests.exceptions.Timeout:
            return "[Timeout – model did not respond]"
        except Exception as e:
            log.exception("LLM Error")
            return f"[Error: {e}]"

    def clear_history(self, sender: str):
        self._histories.pop(sender, None)
