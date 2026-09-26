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
        _client = AsyncOpenAI(api_key=os.getenv("LLM_API_KEY"))
    return _client


async def chat(messages: list, json_mode: bool = False, model_name: str | None = None) -> str:
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    resp = await client().chat.completions.create(
        model=model_name or model(), messages=messages, temperature=0, **kwargs
    )
    return resp.choices[0].message.content or ""


async def chat_json(messages: list, model_name: str | None = None) -> dict:
    raw = await chat(messages, json_mode=True, model_name=model_name)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}
