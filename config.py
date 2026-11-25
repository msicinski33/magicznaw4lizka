"""Central configuration loaded from environment and optional .env file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else ROOT / value


@dataclass(frozen=True)
class Config:
    token: str = os.getenv("DISCORD_TOKEN", "")
    guild_id: int = int(os.getenv("GUILD_ID", "0") or 0)
    database_path: Path = _path("DATABASE_PATH", "data/magic_suitcase.sqlite3")
    creatures_path: Path = _path("CREATURES_PATH", "data/creatures.json")
    assets_dir: Path = _path("ASSETS_DIR", "assets")
    spawn_min_messages: int = int(os.getenv("SPAWN_MIN_MESSAGES", "20"))
    spawn_max_messages: int = int(os.getenv("SPAWN_MAX_MESSAGES", "50"))
    encounter_duration: int = int(os.getenv("ENCOUNTER_DURATION", "120"))
    rarity_weights: dict[str, float] | None = None
    debug: bool = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}


config = Config(rarity_weights=json.loads(os.getenv(
    "RARITY_WEIGHTS", '{"common":60,"uncommon":25,"rare":10,"epic":4,"legendary":0.9,"mythical":0.1}'
)))
