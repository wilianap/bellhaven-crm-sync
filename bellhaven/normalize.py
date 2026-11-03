"""Normalisation helpers used for matching website locations to CRM accounts."""
import re
from difflib import SequenceMatcher

# Map spelled-out street words to one canonical abbreviation so that
# "1125 Logan Boulevard" == "1125 Logan Blvd" and "Barks Road West" == "Barks Rd W".
STREET_WORDS = {
    "road": "rd", "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "lane": "ln", "drive": "dr", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "pike": "pike", "pk": "pike", "pke": "pike", "circle": "cir",
    "terrace": "ter", "square": "sq", "trail": "trl", "way": "way",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

# CRM care_type <- website care offering
CARE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}

# Words that carry no identity in facility names.
NAME_STOP = {"the", "of", "at", "and", "senior", "living", "care", "center", "centre",
             "community", "communities", "home", "health", "healthcare"}


def tokens(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower().replace("&", " and "))


def norm_street(s):
    return " ".join(STREET_WORDS.get(t, t) for t in tokens(s))


def is_po_box(s):
    return bool(re.match(r"^\s*p\.?\s*o\.?\s*box\b", (s or "").lower()))


def norm_zip(z):
    return (z or "").strip()[:5]


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    return d[-10:]


def norm_city(c):
    return " ".join(tokens(c))


def name_key(name, brand="bellhaven"):
    """Distinctive part of a facility name (drops brand and generic words)."""
    toks = [t for t in tokens(name) if t not in NAME_STOP and t != brand]
    toks = ["rehabilitation" if t == "rehab" else t for t in toks]
    return " ".join(toks)


def name_similarity(a, b):
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    return round(SequenceMatcher(None, ka, kb).ratio(), 2)


def care_types(offerings):
    return [CARE_MAP.get(o.strip().lower(), o) for o in offerings]
