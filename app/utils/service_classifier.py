"""Shared service classifier mapping free text to the standard service taxonomy.

Standard service taxonomy (from Scope of Work):
    Battery, Oil Change, Brake, Tire Sales, Tire Rotation,
    Transmission Fluid, Radiator Flush, Fuel System Flush, Other

This module is intentionally self-contained and side-effect free so that
individual scrapers can opt in without changing existing behavior.
"""
from __future__ import annotations

import re
from typing import Optional


STANDARD_SERVICES = [
    "Battery",
    "Oil Change",
    "Brake",
    "Tire Sales",
    "Tire Rotation",
    "Transmission Fluid",
    "Radiator Flush",
    "Fuel System Flush",
    "Other",
]

# Synonyms used to normalize a free-text/legacy category onto the standard label.
_SYNONYMS = {
    "battery": "Battery",
    "batteries": "Battery",
    "alternator": "Battery",

    "oil change": "Oil Change",
    "oil-change": "Oil Change",
    "oil_change": "Oil Change",
    "lube": "Oil Change",
    "synthetic": "Oil Change",
    "synthetic blend": "Oil Change",
    "pennzoil": "Oil Change",
    "full service oil change": "Oil Change",

    "brake": "Brake",
    "brakes": "Brake",
    "brake pad": "Brake",
    "brake pads": "Brake",
    "rotor": "Brake",
    "caliper": "Brake",

    "tire sale": "Tire Sales",
    "tire sales": "Tire Sales",
    "tires": "Tire Sales",
    "tire purchase": "Tire Sales",
    "new tires": "Tire Sales",

    "tire rotation": "Tire Rotation",
    "rotate tires": "Tire Rotation",
    "rotate-tires": "Tire Rotation",

    "transmission": "Transmission Fluid",
    "transmission fluid": "Transmission Fluid",
    "transmission service": "Transmission Fluid",
    "transmission flush": "Transmission Fluid",

    "radiator": "Radiator Flush",
    "radiator flush": "Radiator Flush",
    "radiator fluid": "Radiator Flush",
    "coolant": "Radiator Flush",
    "coolant flush": "Radiator Flush",
    "antifreeze": "Radiator Flush",
    "cooling system": "Radiator Flush",

    "fuel system": "Fuel System Flush",
    "fuel system flush": "Fuel System Flush",
    "fuel system cleaning": "Fuel System Flush",
    "fuel injector": "Fuel System Flush",
    "fuel injection": "Fuel System Flush",

    "exhaust": "Other",
    "seasonal": "Other",
    "inspection": "Other",
    "wiper": "Other",
}

# Keyword patterns scored against the text. Higher priority wins on tie.
# Priority order is important: more specific buckets must precede more generic ones.
_KEYWORD_RULES = [
    # (priority, label, compiled regex)
    (90, "Transmission Fluid", re.compile(r"\btransmission\b", re.IGNORECASE)),
    (90, "Radiator Flush",     re.compile(r"\b(radiator|coolant|antifreeze|cooling\s+system)\b", re.IGNORECASE)),
    (90, "Fuel System Flush",  re.compile(r"\bfuel\s+(system|injector|injection|cleaning|flush|service)\b", re.IGNORECASE)),
    (85, "Battery",            re.compile(r"\b(batter(?:y|ies)|alternator)\b", re.IGNORECASE)),
    (85, "Brake",              re.compile(r"\b(brake|brakes|brake\s+pad|rotor|caliper)\b", re.IGNORECASE)),
    (80, "Tire Rotation",      re.compile(r"\btire\s+rotation\b|\brotate\s+tire", re.IGNORECASE)),
    (75, "Tire Sales",         re.compile(r"\b(tire\s+sale|tire\s+purchase|new\s+tires|buy\s+tires|tires?\s+rebate)\b", re.IGNORECASE)),
    (70, "Tire Sales",         re.compile(r"\btires?\b", re.IGNORECASE)),  # generic "tire" fallback
    (60, "Oil Change",         re.compile(r"\boil\s+change\b|\boil\s+filter\b|\bfull\s+service\s+oil\b|\bsynthetic\s+blend\b|\bpennzoil\b", re.IGNORECASE)),
    (40, "Oil Change",         re.compile(r"\boil\b|\blube\b", re.IGNORECASE)),  # generic oil/lube fallback
]


def normalize_legacy_category(value: Optional[str]) -> Optional[str]:
    """Map a legacy/free-text category onto the standard taxonomy if possible.

    Returns the standard label if a clean synonym is found, otherwise None.
    Use this to migrate existing pipeline output that used lowercase
    informal labels (e.g. "oil change", "brakes") onto the canonical form.
    """
    if not value:
        return None
    key = re.sub(r"[\s\-_]+", " ", value).strip().lower()
    if not key:
        return None
    if key in _SYNONYMS:
        return _SYNONYMS[key]
    if key in {s.lower() for s in STANDARD_SERVICES}:
        for s in STANDARD_SERVICES:
            if s.lower() == key:
                return s
    return None


def classify_service(text: str, hint: Optional[str] = None) -> str:
    """Classify free text (title + body) into one of the 9 standard services.

    `hint` is a pre-seeded label (e.g. service_hint from URL config) and wins
    when provided and valid. Falls back to keyword scoring; returns "Other"
    if no keyword matches.
    """
    if hint:
        normalized = normalize_legacy_category(hint) or (hint if hint in STANDARD_SERVICES else None)
        if normalized:
            return normalized

    if not text:
        return "Other"

    # Score each rule; highest priority winning match wins.
    best_priority = -1
    best_label = "Other"
    for priority, label, pattern in _KEYWORD_RULES:
        if pattern.search(text) and priority > best_priority:
            best_priority = priority
            best_label = label
    return best_label
