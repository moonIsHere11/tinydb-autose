"""Minimal Gemini-API client (stdlib only).

Why not an SDK: Gemma on the Gemini API has no reliable native function
calling and no system-instruction slot, so every agent uses a plain-text
action protocol (see agent.py) and this client only needs generateContent.
The API key is read here and nowhere else; it is never put in a prompt, a
log line or the sandbox environment (ADR-005, ADR-009).
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

WORKERS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-31b-it"]
ROLE_MODELS = {
    # ADR-011 (supersedes ADR-006): Gemma 4 returned 503 "high demand" on most calls during
    # development, so workers use a fast lite model with Gemma as last fallback; the reviewer
    # uses a different, stronger thinking model (verification needs depth more than speed).
    "analyst": WORKERS,
    "builder": WORKERS,
    "tester": WORKERS,
    "reviewer": ["gemini-3-flash-preview", "gemini-3.5-flash"],
    # experiment E1 variant: naive reviewer on the workers' model
    "reviewer_naive": WORKERS,
}


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, role: str, usage: dict, token_budget: int):
        self.role = role
        self.models = list(os.environ.get(f"AUTOSE_MODEL_{role.upper()}", "").split(",")) if os.environ.get(
            f"AUTOSE_MODEL_{role.upper()}") else ROLE_MODELS[role]
        self.key = os.environ["GEMINI_API_KEY"]
        self.usage = usage  # shared dict across the whole job: {"calls","in","out","by_model"}
        self.budget = token_budget

    def _post(self, model: str, body: dict) -> dict:
        req = urllib.request.Request(
            API.format(model=model), data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "x-goog-api-key": self.key})
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.loads(r.read())

    def chat(self, system: str, messages: list[dict]) -> tuple[str, str]:
        """messages: [{"role": "user"|"model", "text": str}]. Returns (text, model)."""
        used = self.usage.get("in", 0) + self.usage.get("out", 0)
        if used > self.budget:
            raise BudgetExceeded(f"token budget {self.budget} exhausted ({used} used)")
        contents = []
        for i, m in enumerate(messages):
            text = m["text"]
            if i == 0:  # Gemma has no system slot: prepend for every model (uniform behaviour)
                text = f"<system>\n{system}\n</system>\n\n{text}"
            contents.append({"role": m["role"], "parts": [{"text": text}]})
        body = {"contents": contents, "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}}
        last_err = None
        for model in self.models:
            for attempt in range(6):
                try:
                    d = self._post(model, body)
                except urllib.error.HTTPError as e:
                    msg = e.read().decode(errors="replace")[:300]
                    last_err = f"{model}: HTTP {e.code} {msg}"
                    if e.code == 429:  # rate limit: back off, same model
                        time.sleep(min(60, 10 * 2 ** attempt) + random.random() * 3)
                        continue
                    if e.code in (500, 503, 504):  # overloaded: one quick retry, then next model
                        if attempt >= 1:
                            break
                        time.sleep(4 + random.random() * 2)
                        continue
                    if e.code == 404:
                        break  # model gone -> next fallback
                    raise RuntimeError(last_err)
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last_err = f"{model}: {e}"
                    time.sleep(5 * (attempt + 1))
                    continue
                cand = (d.get("candidates") or [{}])[0]
                parts = cand.get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                um = d.get("usageMetadata", {})
                self.usage["calls"] = self.usage.get("calls", 0) + 1
                self.usage["in"] = self.usage.get("in", 0) + um.get("promptTokenCount", 0)
                self.usage["out"] = self.usage.get("out", 0) + um.get("candidatesTokenCount", 0) + um.get(
                    "thoughtsTokenCount", 0)
                bm = self.usage.setdefault("by_model", {})
                bm[model] = bm.get(model, 0) + um.get("totalTokenCount", 0)
                if not text.strip():
                    last_err = f"{model}: empty response ({cand.get('finishReason')})"
                    time.sleep(3)
                    continue
                return text, model
        raise RuntimeError(f"all models failed for role {self.role}: {last_err}")
