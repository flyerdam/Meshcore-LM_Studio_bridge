"""
LLM client for querying local or remote chat-completions backends.
"""

import logging
import json
import os
from pathlib import Path
import re
import time
from collections import deque

import requests

from meshcore_bridge.helpers import (
    detect_language,
    sanitize_mesh_reply,
    strip_think_tags,
    strip_tool_artifacts,
)

log = logging.getLogger(__name__)

DEFAULT_OPENAI_COMPAT_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"


class LMStudioClient:

    _TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, url: str, model: str, system_prompt: str,
                 history_len: int = 5,
                 provider: str = "openai_compat",
                 api_key: str | None = None,
                 github_api_version: str = "2026-03-10",
                 model_caps_cache_file: str = "model_capabilities_cache.json",
                 model_caps_cache_ttl_s: int = 86400,
                 token_budget_total: int = 0,
                 token_budget_prompt: int = 0,
                 token_budget_completion: int = 0):
        self.url           = url
        self.model         = model
        self.system_prompt = system_prompt
        self.history_len   = history_len
        self.provider      = provider or "openai_compat"
        self.api_key       = api_key
        self.github_api_version = github_api_version
        self.model_caps_cache_file = model_caps_cache_file
        self.model_caps_cache_ttl_s = max(0, int(model_caps_cache_ttl_s))
        self.token_budget_total = max(0, int(token_budget_total))
        self.token_budget_prompt = max(0, int(token_budget_prompt))
        self.token_budget_completion = max(0, int(token_budget_completion))
        self._used_prompt_tokens = 0
        self._used_completion_tokens = 0
        self._used_total_tokens = 0
        self._histories: dict[str, deque] = {}
        self._model_caps: dict = {"fetched_at": 0, "models": {}}
        self._load_model_caps_cache()
        self._ensure_model_caps_loaded()

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _remaining(limit: int, used: int) -> int | None:
        if limit <= 0:
            return None
        return max(0, limit - used)

    def _budget_block_reason(self, max_tokens_request: int) -> str | None:
        if self.token_budget_total > 0:
            rem_total = self._remaining(self.token_budget_total, self._used_total_tokens)
            if rem_total is not None and rem_total <= 0:
                return "total"
            if rem_total is not None and rem_total < max_tokens_request:
                return "total (remaining too low for requested max_tokens)"
        if self.token_budget_completion > 0:
            rem_completion = self._remaining(self.token_budget_completion, self._used_completion_tokens)
            if rem_completion is not None and rem_completion <= 0:
                return "completion"
            if rem_completion is not None and rem_completion < max_tokens_request:
                return "completion (remaining too low for requested max_tokens)"
        if self.token_budget_prompt > 0:
            rem_prompt = self._remaining(self.token_budget_prompt, self._used_prompt_tokens)
            if rem_prompt is not None and rem_prompt <= 0:
                return "prompt"
        return None

    def _record_usage(self, payload: dict):
        usage = payload.get("usage") or {}
        prompt_tokens = self._safe_int(usage.get("prompt_tokens"), 0)
        completion_tokens = self._safe_int(usage.get("completion_tokens"), 0)
        total_tokens = self._safe_int(usage.get("total_tokens"), prompt_tokens + completion_tokens)

        self._used_prompt_tokens += max(0, prompt_tokens)
        self._used_completion_tokens += max(0, completion_tokens)
        self._used_total_tokens += max(0, total_tokens)

        rem_total = self._remaining(self.token_budget_total, self._used_total_tokens)
        rem_prompt = self._remaining(self.token_budget_prompt, self._used_prompt_tokens)
        rem_completion = self._remaining(self.token_budget_completion, self._used_completion_tokens)

        log.info(
            "TOKEN USAGE req[in=%d out=%d total=%d] session[in=%d out=%d total=%d] remaining[in=%s out=%s total=%s]",
            prompt_tokens,
            completion_tokens,
            total_tokens,
            self._used_prompt_tokens,
            self._used_completion_tokens,
            self._used_total_tokens,
            "∞" if rem_prompt is None else rem_prompt,
            "∞" if rem_completion is None else rem_completion,
            "∞" if rem_total is None else rem_total,
        )

    def _effective_url(self) -> str:
        if self.provider == "github_models":
            if not self.url or self.url == DEFAULT_OPENAI_COMPAT_URL:
                return DEFAULT_GITHUB_MODELS_URL
        return self.url

    def _resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.provider == "github_models":
            return (
                os.getenv("GITHUB_TOKEN")
                or os.getenv("GH_MODELS_TOKEN")
                or os.getenv("LLM_API_KEY")
            )
        return os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._resolved_api_key()
        if self.provider == "github_models":
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = self.github_api_version
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            return headers
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _cache_path(self) -> Path:
        p = Path(self.model_caps_cache_file)
        if p.is_absolute():
            return p
        return Path.cwd() / p

    def _load_model_caps_cache(self):
        try:
            p = self._cache_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("models"), dict):
                self._model_caps = data
        except Exception:
            log.debug("Could not load model capabilities cache", exc_info=True)

    def _save_model_caps_cache(self):
        try:
            p = self._cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._model_caps, indent=2), encoding="utf-8")
        except Exception:
            log.debug("Could not save model capabilities cache", exc_info=True)

    @staticmethod
    def _infer_caps_from_model_id(model_id: str) -> dict:
        """Best-effort static capabilities for known model families."""
        mid = (model_id or "").lower()
        caps = {
            "token_param": "max_tokens",
            "supports_temperature": True,
        }
        # GPT-5 family on GitHub Models expects max_completion_tokens
        # and often only default temperature.
        if "gpt-5" in mid:
            caps["token_param"] = "max_completion_tokens"
            caps["supports_temperature"] = False
        return caps

    def _fetch_github_model_catalog(self):
        if self.provider != "github_models":
            return
        token = self._resolved_api_key()
        if not token:
            return
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": self.github_api_version,
        }
        try:
            resp = requests.get(
                "https://models.github.ai/catalog/models",
                headers=headers,
                timeout=20,
            )
            if resp.status_code != 200:
                log.debug("Model catalog fetch failed: HTTP %d", resp.status_code)
                return
            payload = resp.json()
            items = payload if isinstance(payload, list) else payload.get("data", [])
            models: dict[str, dict] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or item.get("name") or "").strip()
                if not model_id:
                    continue
                caps = self._infer_caps_from_model_id(model_id)
                models[model_id] = {
                    "caps": caps,
                    "raw": {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "publisher": item.get("publisher"),
                    },
                }
            if models:
                self._model_caps = {
                    "fetched_at": int(time.time()),
                    "models": models,
                }
                self._save_model_caps_cache()
                log.info("Model capabilities cache refreshed: %d models", len(models))
        except Exception:
            log.debug("Model catalog fetch failed", exc_info=True)

    def _ensure_model_caps_loaded(self):
        if self.provider != "github_models":
            return
        fetched_at = self._safe_int(self._model_caps.get("fetched_at"), 0)
        is_stale = (time.time() - fetched_at) > self.model_caps_cache_ttl_s if fetched_at else True
        if is_stale:
            self._fetch_github_model_catalog()

        # Ensure current model always has a capability entry.
        models = self._model_caps.setdefault("models", {})
        if self.model not in models:
            models[self.model] = {"caps": self._infer_caps_from_model_id(self.model)}
            self._model_caps["fetched_at"] = int(time.time())
            self._save_model_caps_cache()

    def _model_caps_for_current(self) -> dict:
        models = self._model_caps.get("models", {})
        entry = models.get(self.model, {})
        caps = entry.get("caps") if isinstance(entry, dict) else None
        return caps if isinstance(caps, dict) else self._infer_caps_from_model_id(self.model)

    def _post_chat(self, messages: list[dict], max_tokens: int,
                   temperature: float, timeout: int) -> requests.Response | None:
        if self.provider == "github_models" and not self._resolved_api_key():
            return None

        self._ensure_model_caps_loaded()
        caps = self._model_caps_for_current()
        token_param = caps.get("token_param", "max_tokens")
        supports_temperature = bool(caps.get("supports_temperature", True))

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        payload[token_param] = max_tokens
        if supports_temperature:
            payload["temperature"] = temperature

        url = self._effective_url()
        log.debug("API POST %s model=%s %s=%d temp=%s",
                  url, self.model, token_param, max_tokens,
                  payload.get("temperature", "n/a"))
        return requests.post(
            url,
            json=payload,
            headers=self._request_headers(),
            timeout=timeout,
        )

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

        off       – never auto-reply
        minimal   – only if message has an AI keyword AND local LLM confirms
        keyword   – AI keywords, question marks, bot-address keywords (no LLM)
        normal    – local LLM decides for every message
        aggressive – reply to almost anything (very permissive, no LLM needed)
        """
        sender_lower = (sender or "").strip().lower()  # noqa: F841 (kept for compat)

        text = (message or "").strip()
        if not text or intensity == "off":
            return False

        lower = text.lower()
        norm = "".join(ch for ch in lower if ch.isalnum())
        aliases = [a for a in (mention_aliases or []) if a]

        # Always: explicit !ai prefix → yes in any mode.
        if lower.startswith("!ai"):
            log.info("AUTO-GATE YES: explicit !ai prefix")
            return True

        # Always: direct name/alias mention → yes in any non-off mode.
        if any(a in norm for a in aliases):
            log.info("AUTO-GATE YES: name/alias mention")
            return True

        # ── minimal: AI keyword present, then LLM gate confirms ──────────────
        if intensity == "minimal":
            has_ai_kw = bool(re.search(r"\b(ai|sztuczna|intelig|asyst|artific|bot)\b", lower))
            if not has_ai_kw:
                log.info("AUTO-GATE NO [minimal]: no AI keyword")
                return False
            log.info("AUTO-GATE [minimal]: AI keyword found, asking LLM [%s]", self.model)
            return self._gate_via_llm(text, channel_context)

        # ── keyword: static rules only, no LLM, no followup heuristics ───────
        if intensity == "keyword":
            if re.search(r"\b(bocie|asystencie|assistant|asystent)\b", lower):
                log.info("AUTO-GATE YES [keyword]: bot-address keyword")
                return True
            if "?" in lower:
                log.info("AUTO-GATE YES [keyword]: question mark")
                return True
            if re.search(r"\b(ai|sztuczna|intelig|robot|bot)\b", lower):
                log.info("AUTO-GATE YES [keyword]: AI keyword")
                return True
            log.info("AUTO-GATE NO [keyword]: no keywords matched")
            return False

        # ── normal: LLM gate decides ──────────────────────────────────────────
        if intensity == "normal":
            log.info("AUTO-GATE [normal]: delegating to LLM [%s]", self.model)
            return self._gate_via_llm(text, channel_context)

        # ── aggressive: reply to almost everything non-trivial ────────────────
        if intensity == "aggressive":
            if len(text) < 4:
                log.info("AUTO-GATE NO [aggressive]: message too short")
                return False
            log.info("AUTO-GATE YES [aggressive]")
            return True

        # legacy aliases from old config
        if intensity == "cautious":
            return False

        return False

    @staticmethod
    def _looks_like_followup(sender_lower: str, message_lower: str,
                             channel_context: list[dict] | None) -> bool:
        """Detect short follow-ups after a recent AI-adjacent exchange.

        Makes `normal` mode a bit proactive without turning random chatter on.
        """
        if not channel_context or not sender_lower:
            return False

        followup_markers = (
            "fajny", "super", "ok", "okej", "git", "spoko", "dzieki", "dzięki",
            "a po polsku", "po polsku", "krocej", "krócej", "jeszcze", "mozesz",
            "możesz", "inaczej", "ladnie", "ładnie", "slabo", "słabo",
        )
        if len(message_lower) > 80 and "?" not in message_lower:
            return False
        if not any(marker in message_lower for marker in followup_markers) and "?" not in message_lower:
            return False

        same_sender_recent = [
            str(m.get("text", "")).lower()
            for m in channel_context[-4:]
            if str(m.get("sender", "")).strip().lower() == sender_lower
        ]
        if not same_sender_recent:
            return False

        ai_adjacent = re.compile(r"\b(ai|asystent|assistant|bot|robot)\b|!ai|\?")
        return any(ai_adjacent.search(text) for text in same_sender_recent)

    def _gate_via_llm(self, message: str,
                      channel_context: list[dict] | None = None) -> bool:
        """Lightweight LLM gate: returns True if model says YES."""
        reason = self._budget_block_reason(max_tokens_request=50)
        if reason:
            log.info("AUTO-GATE skipped: token budget reached (%s)", reason)
            return False
        log.info("AUTO-GATE LLM call -> %s [%s] msg='%s'",
                 self._effective_url(), self.model, message[:60])
        ctx_lines = ""
        if channel_context:
            ctx_lines = "\n".join(
                f"{m['sender']}: {m['text']}" for m in channel_context[-5:]
            )
            ctx_lines = f"Recent channel messages:\n{ctx_lines}\n\n"

        prompt = (
            f"{ctx_lines}"
            f"New message: {message}\n\n"
            "Should you respond to this message? Is it related to you or can be related to previous conversation?\n"
            "Answer YES or NO only."
        )
        try:
            resp = self._post_chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a gatekeeper for an AI radio channel cool dude skater boi assistant. "
                            "Decide if its cool to reply to the new message based on its content and relevance. "
                            "Reply YES if it's a question to you, a request for you, or directly "
                            "addresses an AI. Reply NO for unrelated chat, "
                            "messages addressed to other people, or noise. "
                            "Reply NO for messages that are likely just commands for other bots, like test, path, snr, weather, news, search, channel, or !bot commands. "
                            "Just be cool, don't overthink it. Answer quickly and concisely. "
                            "Also don't be afraid to say YES if it's borderline but could be worth replying to (even for laughter or fun). "
                            "Also don't interrupt normal human chatter with AI replies if it's not worth it - silence is better than noise in radio communication. "
                            "Dont respond to messages that are meant for other bots or assistants - like !bot commands or other AI assistants. you are smarter and meant for greater things"
                            "Output ONLY the word YES or NO."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=50,
                temperature=0.1,
                timeout=15,
            )
            if resp is None:
                return False
            if resp.status_code != 200:
                return False
            payload = resp.json()
            self._record_usage(payload)
            content = payload["choices"][0]["message"]["content"].strip()
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

        lang = detect_language(question)
        lang_hint = f"[REPLY IN {lang.upper()} ONLY. Do not switch language.]\n" if lang else ""

        # Build context preamble to prepend to the user question.
        ctx_preamble = ""
        if channel_context:
            ctx_lines = "\n".join(
                f"{m['sender']}: {m['text']}" for m in channel_context
            )
            ctx_preamble = f"[Recent channel messages]\n{ctx_lines}\n\n"

        if save_history:
            messages.extend(list(hist))
            # Append current question (without context/hint in stored history)
            # Build the enriched user message for this API call only
            enriched = lang_hint + ctx_preamble + question
            messages.append({"role": "user", "content": enriched})
            # Store plain question in history (no context pollution)
            hist.append({"role": "user", "content": question})
        else:
            enriched = lang_hint + ctx_preamble + question
            messages.append({"role": "user", "content": enriched})
        try:
            reason = self._budget_block_reason(max_tokens_request=300)
            if reason:
                return f"[Token budget reached: {reason}]"
            resp = self._post_chat(
                messages=messages,
                max_tokens=300,
                temperature=0.7,
                timeout=60,
            )
            if resp is None:
                if self.provider == "github_models":
                    return "[Missing GitHub Models token]"
                return "[Missing LLM API key]"
            if resp.status_code != 200:
                log.error("LLM HTTP %d: %s", resp.status_code, resp.text[:200])
                if resp.status_code in self._TRANSIENT_HTTP_STATUSES:
                    return "[AI backend temporarily unavailable - try again]"
                return f"[HTTP Error {resp.status_code}]"
            payload = resp.json()
            self._record_usage(payload)
            content = payload["choices"][0]["message"]["content"].strip()
            content = strip_think_tags(content)
            content = strip_tool_artifacts(content)
            content = sanitize_mesh_reply(content)
            if save_history:
                hist.append({"role": "assistant", "content": content})
            return content
        except requests.exceptions.ConnectionError:
            return "[LLM backend unavailable]"
        except requests.exceptions.Timeout:
            return "[Timeout – model did not respond]"
        except Exception as e:
            log.exception("LLM Error")
            return f"[Error: {e}]"

    def clear_history(self, sender: str):
        self._histories.pop(sender, None)
