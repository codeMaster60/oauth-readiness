"""Groq client tuned for the FREE tier.

Free-tier limits are per model (e.g. ~8K tokens/minute each), so one model
alone makes a guide slow. We keep a pool of models, track tokens used per model
in a sliding 60s window, and send each call to the model with the most
headroom. On 429 we honour `retry-after`; models that are unavailable
(decommissioned / not enabled) are dropped from the pool for the session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from typing import Awaitable, Callable, Optional

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_EXTRACT = "openai/gpt-oss-20b,qwen/qwen3-32b,llama-3.3-70b-versatile,openai/gpt-oss-120b"
DEFAULT_SYNTH = "openai/gpt-oss-120b,llama-3.3-70b-versatile,openai/gpt-oss-20b"

Notify = Callable[[str], Awaitable[None]]


class LLMError(RuntimeError):
    pass


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def parse_json(text: str) -> dict:
    """Lenient JSON parse: strips code fences / prose around the object."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def retry_seconds(text: str) -> float:
    """Groq says 'try again in 12m3.5s' or '1h2m' or '7.2s'. Read all of it, not just the seconds."""
    m = re.search(r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", text or "")
    if not m or not any(m.groups()):
        return 0.0
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)


class ModelPool:
    def __init__(self, models: list[str], tpm: int):
        self.models = [m.strip() for m in models if m.strip()]
        self.tpm = tpm
        self.usage: dict[str, deque] = {m: deque() for m in self.models}
        self.blocked_until: dict[str, float] = {}
        self.daily: set[str] = set()  # models that hit the per-day cap, not the per-minute one

    def _used(self, m: str) -> int:
        q, now = self.usage[m], time.time()
        while q and now - q[0][0] > 60:
            q.popleft()
        return sum(t for _, t in q)

    def pick(self, need: int) -> tuple[Optional[str], float]:
        """Best model for `need` tokens, or (None, seconds_to_wait)."""
        now, best, best_free, wait = time.time(), None, -1, float("inf")
        for m in self.models:
            if self.blocked_until.get(m, 0) > now:
                wait = min(wait, self.blocked_until[m] - now)  # a 12-minute daily cooldown stays 12 minutes
                continue
            free = self.tpm - self._used(m)
            if free >= need and free > best_free:
                best, best_free = m, free
            elif self.usage[m]:
                wait = min(wait, 60 - (now - self.usage[m][0][0]) + 0.5)
            else:
                wait = min(wait, 60.0)  # request bigger than a model's whole minute budget: try again later
        return best, max(0.5, 60.0 if wait == float("inf") else wait)

    def record(self, m: str, tokens: int):
        self.usage[m].append((time.time(), tokens))

    def block(self, m: str, seconds: float):
        self.blocked_until[m] = time.time() + seconds

    def drop(self, m: str):
        if m in self.models and len(self.models) > 1:
            self.models.remove(m)


class Groq:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        tpm = int(os.getenv("GROQ_TPM_PER_MODEL", "7500"))
        self.extract_pool = ModelPool(os.getenv("GROQ_EXTRACT_MODELS", DEFAULT_EXTRACT).split(","), tpm)
        self.synth_pool = ModelPool(os.getenv("GROQ_SYNTH_MODELS", DEFAULT_SYNTH).split(","), tpm)
        # Both pools share the same per-model quota on Groq's side.
        shared = {**self.extract_pool.usage, **self.synth_pool.usage}
        self.extract_pool.usage = self.synth_pool.usage = {m: shared.get(m, deque()) for m in shared}
        self.client = httpx.AsyncClient(timeout=90)
        self.daily_until = 0.0  # set when every model in a pool is past its per-day cap

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def out_of_quota(self) -> bool:
        """True while the free tier's daily cap is known to be hit, so the site can say so up front."""
        return time.time() < self.daily_until

    async def json_call(
        self,
        system: str,
        user: str,
        *,
        pool: str = "extract",
        max_tokens: int = 1200,
        notify: Optional[Notify] = None,
    ) -> tuple[dict, str]:
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set")
        if self.out_of_quota:
            raise LLMError("daily")
        p = self.extract_pool if pool == "extract" else self.synth_pool
        need = _est_tokens(system) + _est_tokens(user) + max_tokens
        last_err = "no attempt"
        # Waiting for free-tier capacity is not a failure, so it doesn't use up attempts. Two runs at once can
        # spend minutes just waiting their turn; we give up only on a time budget or after real errors.
        deadline = time.time() + float(os.getenv("LLM_CALL_BUDGET_S", "300"))
        attempts, last_notice = 0, 0.0
        while attempts < 8 and time.time() < deadline:
            model, wait = p.pick(need)
            if model is None and p.daily and wait > 20:
                # Every model is blocked and at least one hit the per-day cap: no point waiting minutes for it.
                self.daily_until = max(self.daily_until, time.time() + wait)
                raise LLMError("daily")
            if model is None and wait > deadline - time.time():
                raise LLMError("busy")
            if model is None:
                if notify and time.time() - last_notice > 20:
                    await notify(f"Waiting for free model capacity (about {wait:.0f}s). Busy when two runs share it.")
                    last_notice = time.time()
                await asyncio.sleep(min(wait, max(0.5, deadline - time.time())))
                continue
            attempts += 1
            body = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            if model.startswith("openai/gpt-oss"):
                body["reasoning_effort"] = "low"
            if model.startswith("qwen/"):
                body["reasoning_format"] = "hidden"
            p.record(model, need)
            try:
                r = await self.client.post(
                    GROQ_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
                )
            except httpx.HTTPError as e:
                last_err = f"network: {e}"
                await asyncio.sleep(2)
                continue
            if r.status_code == 429:
                ra = float(r.headers.get("retry-after", "0") or 0)
                secs = max(ra, retry_seconds(r.text), 3)
                p.block(model, secs)
                if re.search(r"per day|\bTPD\b|\bRPD\b", r.text):
                    p.daily.add(model)
                last_err = f"429 on {model}"
                continue
            if r.status_code in (400, 404) and re.search(
                r"decommission|does not exist|not found|model_not|not have access|blocked", r.text, re.I
            ):
                p.drop(model)
                last_err = f"{model} unavailable"
                continue
            if r.status_code == 400 and "json_validate_failed" in r.text:
                # Model produced near-JSON; salvage it if Groq echoed it back.
                failed = r.json().get("error", {}).get("failed_generation", "")
                try:
                    return parse_json(failed), model
                except Exception:
                    last_err = "invalid JSON"
                    continue
            if r.status_code >= 400:
                last_err = f"{r.status_code}: {r.text[:200]}"
                await asyncio.sleep(1.5)
                continue
            data = r.json()
            used = data.get("usage", {}).get("total_tokens")
            if used:  # replace estimate with real usage
                p.usage[model][-1] = (time.time(), used)
            content = data["choices"][0]["message"].get("content") or ""
            try:
                return parse_json(content), model
            except Exception:
                last_err = "unparseable JSON"
                continue
        if time.time() >= deadline and attempts == 0:
            raise LLMError("busy")
        raise LLMError(f"model error ({last_err})")
