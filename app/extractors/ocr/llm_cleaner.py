"""LLM-based text cleaning for OCR output."""
import os
import requests
import json
from typing import Optional
from app.config.constants import OPENAI_API_KEY
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


def clean_promo_text_with_llm(ocr_text: str, context: str = "") -> Optional[str]:
    """Clean and extract structured promo information using OpenAI LLM."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set, skipping LLM cleaning")
        return None
    
    if not ocr_text or len(ocr_text.strip()) < 10:
        return None
    
    try:
        prompt = f"""Extract promotion details from this OCR text from an automotive service coupon image. 
Return ONLY a clean JSON object with these fields:
{{
    "service_name": "oil change/brake/battery/etc",
    "promo_description": "clean description",
    "discount_value": "$X or X% or free",
    "coupon_code": "code if present",
    "expiry_date": "date if present",
    "category": "oil change/brakes/battery/seasonal/etc"
}}

OCR Text:
{ocr_text}

Context: {context}

Return only the JSON, no other text."""

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Try multiple OpenAI models (prefer newer, fallback to cheaper)
        model_options = [
            "gpt-4o-mini",  # Fast and cost-effective
            "gpt-4-turbo",  # More capable
            "gpt-4",        # Fallback
            "gpt-3.5-turbo" # Cheapest fallback
        ]
        
        response = None
        last_error = None
        
        for model_name in model_options:
            try:
                data = {
                    "model": model_name,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a helpful assistant that extracts structured promotion data from OCR text or promotional content. Return only valid JSON, no additional text or explanations."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"}  # Force JSON response
                }
                
                response = requests.post(OPENAI_API_URL, headers=headers, json=data, timeout=30)
                
                if response.ok:
                    # Success, break and process response
                    break
                else:
                    error_data = response.json() if response.status_code != 200 else {}
                    last_error = error_data
                    # If it's not a model error, don't try other models
                    if "invalid_request_error" not in str(error_data).lower() or "model" not in str(error_data).lower():
                        break
            except Exception as e:
                last_error = str(e)
                if model_name != model_options[-1]:  # Don't break on last model
                    continue
                break
        
        # If we got here without a successful response, raise error
        if not response or not response.ok:
            error_detail = response.text if response and hasattr(response, 'text') else str(last_error)
            logger.error(f"OpenAI API error: {error_detail[:200]}")
            if response:
                response.raise_for_status()
            else:
                raise Exception(f"OpenAI API request failed: {last_error}")
        
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Clean up JSON response (OpenAI may wrap it or return raw JSON)
        content = content.strip()
        
        # Remove markdown code blocks if present
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        # Try to parse JSON
        try:
            parsed = json.loads(content)
            logger.debug(f"OpenAI LLM cleaned promo text: {parsed}")
            return parsed
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse OpenAI LLM response as JSON: {e}")
            logger.warning(f"Response content: {content[:200]}")
            # Try to extract JSON if it's embedded in text
            import re
            json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                    logger.info("Successfully extracted JSON from embedded text")
                    return parsed
                except:
                    pass
            return None
            
    except Exception as e:
        logger.error(f"Error cleaning text with OpenAI LLM: {e}")
        return None

