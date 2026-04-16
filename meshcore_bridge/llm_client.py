"""
LM Studio (OpenAI-compatible) client for querying a local LLM.
"""

import logging
from collections import deque

import requests

from meshcore_bridge.helpers import strip_think_tags

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
            channel_context: list[dict] | None = None) -> str:
        return self._call(sender, question,
                          save_history=True, channel_context=channel_context)

    def analyze(self, prompt: str) -> str:
        return self._call("__analysis__", prompt, save_history=False)

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
