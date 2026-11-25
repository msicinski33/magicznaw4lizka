"""Game rules shared by Discord commands and tests."""
from __future__ import annotations

import random
import unicodedata

RARITIES = ("common", "uncommon", "rare", "epic", "legendary", "mythical")


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.strip().casefold())
    value = "".join(c for c in value if not unicodedata.combining(c)).replace("ł", "l").replace("đ", "d")
    return value.replace("’", "'")


def matches_guess(guess: str, names: list[str]) -> bool:
    normalized = normalize_name(guess)
    if len(normalized) < 4:
        return False
    return normalized in {normalize_name(name) for name in names if len(normalize_name(name)) >= 4}


def roll_threshold(minimum: int, maximum: int, rng=random) -> int:
    if minimum < 1 or maximum < minimum:
        raise ValueError("Nieprawidłowy zakres progu wiadomości")
    return rng.randint(minimum, maximum)


def weighted_creature(creatures: list[dict], rarity_weights: dict[str, float], rng=random) -> dict:
    eligible = [c for c in creatures if c.get("enabled", 1) and rarity_weights.get(c["rarity"].lower(), 0) > 0]
    if not eligible:
        raise ValueError("Brak aktywnych stworzeń z dodatnią wagą rzadkości")
    weights = [rarity_weights.get(c["rarity"].lower(), 0) for c in eligible]
    return rng.choices(eligible, weights=weights, k=1)[0]
