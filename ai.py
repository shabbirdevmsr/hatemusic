"""Ask Groq to generate a fresh title + description."""

import json
import requests

from config import GROQ_API_KEY, GROQ_MODEL


SYSTEM_PROMPT = """You are a YouTube SEO assistant. You will receive the original
title and description of a song video. The new video uses a vocals-only (acapella)
version of the same song. Generate:
  1. A catchy, click-worthy YouTube title (max 100 chars).
  2. A YouTube description (300-800 chars) with emojis, hashtags, and a hint
     that it's vocals-only / no music.
Return ONLY valid JSON, no markdown, shaped exactly like:
{"title": "...", "description": "..."}"""


def generate_metadata(original_title: str, original_desc: str,
                      custom_instruction: str = None) -> dict:
    user_prompt = (
        f"Original title:\n{original_title}\n\n"
        f"Original description:\n{original_desc[:1500]}"
    )
    if custom_instruction:
        user_prompt += (
            f"\n\nAdditional instruction from the user:\n{custom_instruction}"
        )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
        },
        json=payload, timeout=60,
    )
    r.raise_for_status()
    result = r.json()
    parsed = json.loads(result["choices"][0]["message"]["content"])
    return {
        "title":       parsed.get("title", "").strip(),
        "description": parsed.get("description", "").strip(),
    }
