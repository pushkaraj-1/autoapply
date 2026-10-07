"""OpenRouter chat calls. Keys come from the project .env file."""

import os

import httpx
from dotenv import load_dotenv

from jobautomate.profile import ROOT

load_dotenv(ROOT / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Essays and cover letters use this stronger model; short answers use OPENROUTER_MODEL.
WRITING_MODEL = os.environ.get("OPENROUTER_WRITING_MODEL")


def chat(messages: list[dict], temperature: float = 0.4, max_tokens: int = 2000, model: str | None = None) -> str:
    response = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_KEY']}"},
        json={
            "model": model or os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite"),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()
