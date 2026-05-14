"""LLM-based text cleaning for OCR / promo text extraction.

Tries Anthropic Claude first (if ANTHROPIC_API_KEY is set), falls back to
OpenAI. Returns a dict matching the legacy contract, or None.
"""
import json
import re
import requests
from typing import Optional

from app.config.constants import (
    OPENAI_API_KEY,
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
)
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


def _build_prompt(ocr_text: str, context: str = "") -> str:
    return f"""Extract promotion details from this OCR/HTML text from an automotive service coupon.

Return ONLY a clean JSON object with these fields (use null when unknown):
{{
    "service_name": "oil change | brake | battery | tire sales | tire rotation | transmission fluid | radiator flush | fuel system flush | other",
    "promo_description": "clean customer-facing description (1-2 sentences, no UI noise)",
    "discount_value": "$X or X% or 'free' or null",
    "coupon_code": "ALPHANUMERIC code (omit common english words like LINK/PER/COUPON/OFF)",
    "expiry_date": "YYYY-MM-DD or short human-readable, or null",
    "category": "same as service_name"
}}

Coupon Text:
{ocr_text}

Context: {context}

Return ONLY the JSON object, no markdown, no explanation."""


def _parse_json_blob(content: str) -> Optional[dict]:
    """Strip common wrappers and parse JSON. Returns dict, or None."""
    if not content:
        return None
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, list):
        parsed = next((item for item in parsed if isinstance(item, dict)), None)
    if not isinstance(parsed, dict):
        return None
    return parsed


def _call_anthropic(ocr_text: str, context: str = "") -> Optional[dict]:
    """Call Claude (Anthropic Messages API). Returns parsed dict or None."""
    if not ANTHROPIC_API_KEY:
        return None

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
        "temperature": 0.1,
        "system": (
            "You extract structured promotion data from messy OCR/HTML text "
            "for automotive service competitors. Return only valid JSON."
        ),
        "messages": [{"role": "user", "content": _build_prompt(ocr_text, context)}],
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Anthropic request failed: {exc}")
        return None

    if not resp.ok:
        # If model name is invalid try one known-good fallback model
        try:
            err = resp.json()
        except Exception:  # noqa: BLE001
            err = {"raw": resp.text[:200]}
        logger.warning(f"Anthropic API non-200 ({resp.status_code}): {str(err)[:200]}")
        if resp.status_code == 404 and ANTHROPIC_MODEL != "claude-3-5-sonnet-latest":
            body["model"] = "claude-3-5-sonnet-latest"
            try:
                resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Anthropic retry failed: {exc}")
                return None
            if not resp.ok:
                logger.warning(
                    f"Anthropic retry non-200 ({resp.status_code}): {resp.text[:200]}"
                )
                return None
        else:
            return None

    try:
        data = resp.json()
    except ValueError:
        return None

    blocks = data.get("content") or []
    if not blocks:
        return None
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    return _parse_json_blob(text)


def _call_openai(ocr_text: str, context: str = "") -> Optional[dict]:
    """Call OpenAI Chat Completions. Returns parsed dict or None."""
    if not OPENAI_API_KEY:
        return None

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    prompt = _build_prompt(ocr_text, context)

    model_options = ["gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"]

    response = None
    last_error = None
    for model_name in model_options:
        try:
            body = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": "You extract structured promotion data. Return only valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 500,
                "response_format": {"type": "json_object"},
            }
            response = requests.post(OPENAI_API_URL, headers=headers, json=body, timeout=30)
            if response.ok:
                break
            last_error = response.text[:200]
            # Only iterate to next model if it's specifically a model error
            if "model" not in last_error.lower():
                break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue

    if not response or not response.ok:
        logger.warning(f"OpenAI API failed: {str(last_error)[:200]}")
        return None

    try:
        data = response.json()
    except ValueError:
        return None
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_json_blob(content)


def clean_promo_text_with_llm(ocr_text: str, context: str = "") -> Optional[dict]:
    """Clean / structure promo text via Anthropic (primary) → OpenAI (fallback).

    Returns a dict with keys: service_name, promo_description, discount_value,
    coupon_code, expiry_date, category. Returns None on total failure.
    """
    if not ocr_text or len(ocr_text.strip()) < 10:
        return None

    if ANTHROPIC_API_KEY:
        parsed = _call_anthropic(ocr_text, context)
        if parsed:
            logger.debug(f"LLM (Anthropic) cleaned promo: {parsed}")
            return parsed
        logger.info("Anthropic returned no parseable result; falling back to OpenAI.")

    if OPENAI_API_KEY:
        parsed = _call_openai(ocr_text, context)
        if parsed:
            logger.debug(f"LLM (OpenAI) cleaned promo: {parsed}")
            return parsed

    if not ANTHROPIC_API_KEY and not OPENAI_API_KEY:
        logger.warning("Neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set; skipping LLM cleaning")
    return None
