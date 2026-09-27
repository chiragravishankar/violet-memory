"""Thin OpenAI client using the same LLM_API_KEY and LLM_MODEL as Cognee."""

import json
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

_client: AsyncOpenAI | None = None


def _bare(m: str) -> str:
    # Cognee accepts "openai/gpt-4o-mini"; the OpenAI SDK wants just "gpt-4o-mini".
    return m.split("/", 1)[1] if m.startswith("openai/") else m


def model() -> str:
    return _bare(os.getenv("LLM_MODEL", "gpt-4o-mini"))


def extract_model() -> str:
    """Model for deadline extraction. Optional EXTRACT_MODEL, else LLM_MODEL."""
    return _bare(os.getenv("EXTRACT_MODEL") or os.getenv("LLM_MODEL", "gpt-4o-mini"))


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        # The SDK's default timeout is 10 minutes: one stuck request would freeze a reply that long.
        _client = AsyncOpenAI(api_key=os.getenv("LLM_API_KEY"), timeout=60, max_retries=2)
    return _client


def track(resp) -> None:
    """Count a completion's tokens against the user whose message is being handled."""
    import users

    u = getattr(resp, "usage", None)
    if u is not None:
        users.record(users.acting.get(), "tokens", getattr(u, "total_tokens", 0) or 0)


async def chat(messages: list, json_mode: bool = False, model_name: str | None = None,
               temperature: float = 0) -> str:
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    resp = await client().chat.completions.create(
        model=model_name or model(), messages=messages, temperature=temperature, **kwargs
    )
    track(resp)
    return resp.choices[0].message.content or ""


async def chat_json(messages: list, model_name: str | None = None, temperature: float = 0) -> dict:
    raw = await chat(messages, json_mode=True, model_name=model_name, temperature=temperature)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}
