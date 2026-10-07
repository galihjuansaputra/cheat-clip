import logging
import os
import re
from typing import List, Optional

from google import genai

from backend.config import logger

KNOWN_FLASH_MODELS = [
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash',
    'gemini-1.5-flash-8b',
]


def parse_gemini_model_sort_key(name: str):
    """Sort key for Gemini models: parses major and minor versions (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5),
    tier (standard > lite/8b > preview/exp), so newest and most capable models come first."""
    name_clean = (name or "").split('/')[-1].lower()
    m = re.search(r'(\d+)(?:\.(\d+))?', name_clean)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) is not None else 0
    else:
        major, minor = 0, 0

    if 'lite' in name_clean or '8b' in name_clean:
        tier = 2
    elif 'exp' in name_clean or 'preview' in name_clean:
        tier = 1
    else:
        tier = 3

    return (major, minor, tier, name_clean)


def get_flash_models_for_key(client: genai.Client) -> List[str]:
    """Dynamically query all available flash models for the given API key.
    Discovers newer versions (e.g., 3.7, 3.6, 3.5) and earlier versions (2.5, 2.0, 1.5),
    merging with known fallback models and sorting in descending order of version/capability."""
    discovered = []
    try:
        models_page = client.models.list()
        for m in models_page:
            name = m.name or ""
            short_name = name.split('/')[-1]
            if "gemini" in short_name.lower() and "flash" in short_name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                # Exclude non-text, specialized, or non-generative tasks
                exclude_keywords = [
                    'tuning', 'thinking', 'vision', 'image', 'tts',
                    'omni', 'customtools', 'embed', 'realtime', 'robotics'
                ]
                if not any(x in short_name.lower() for x in exclude_keywords):
                    if short_name not in discovered:
                        discovered.append(short_name)
    except Exception as e:
        logger.warning(f"Could not dynamically list models: {e}")

    # Combine discovered with known flash models, preserving uniqueness
    combined_pool = list(dict.fromkeys(discovered + KNOWN_FLASH_MODELS))
    # Sort descending so newest versions (3.7, 3.6, 3.5, 2.5, 2.0, 1.5) are prioritized
    ordered = sorted(combined_pool, key=parse_gemini_model_sort_key, reverse=True)
    return ordered


def list_available_gemini_models(api_key: str = "") -> List[str]:
    """Fetches list of available Gemini models using the user's API key, prioritizing Flash models (newest first)."""
    default_models = [
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
        'gemini-2.0-flash',
        'gemini-2.0-flash-lite',
        'gemini-1.5-flash',
        'gemini-2.5-pro'
    ]
    key_to_use = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key_to_use or key_to_use.lower() == "mock":
        return default_models
    try:
        client = genai.Client(api_key=key_to_use)
        models_page = client.models.list()
        
        flash_models = []
        pro_models = []
        other_models = []
        
        for m in models_page:
            name = m.name or ""
            if "gemini" in name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                
                short_name = name.split('/')[-1]
                exclude_keywords = [
                    'tuning', 'thinking', 'vision', 'image', 'tts',
                    'omni', 'customtools', 'embed', 'realtime', 'robotics'
                ]
                if any(x in short_name.lower() for x in exclude_keywords):
                    continue
                
                if "flash" in short_name.lower():
                    if short_name not in flash_models:
                        flash_models.append(short_name)
                elif "pro" in short_name.lower():
                    if short_name not in pro_models:
                        pro_models.append(short_name)
                elif any(x in short_name.lower() for x in ['lite', 'exp']):
                    if short_name not in other_models:
                        other_models.append(short_name)
        
        # Sort flash models by version descending (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5)
        ordered_flash = sorted(
            list(dict.fromkeys(flash_models + KNOWN_FLASH_MODELS)),
            key=parse_gemini_model_sort_key,
            reverse=True
        )
        ordered_pro = sorted(pro_models, key=parse_gemini_model_sort_key, reverse=True)
        ordered_other = sorted(other_models, key=parse_gemini_model_sort_key, reverse=True)
        
        final_list = ordered_flash + ordered_pro + ordered_other
        if not final_list:
            final_list = default_models
            
        return final_list
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        return default_models
