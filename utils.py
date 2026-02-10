"""
Crypto Swarm Intelligence System - Shared Utilities
"""
import json
import logging
import os
from datetime import datetime, timezone

import config


def setup_logging(level=logging.INFO):
    """Configure logging for the swarm system."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def load_json(filepath: str) -> dict | list:
    """Load JSON from file, return empty dict if not found."""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r") as f:
        return json.load(f)


def save_json(filepath: str, data: dict | list):
    """Save data to JSON file, creating directories if needed."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)


def format_usd(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.1f}K"
    return f"${amount:.2f}"


def format_pct(value: float) -> str:
    return f"{value:+.1f}%"


def format_score(score: float) -> str:
    return f"{score:.1f}/10"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hours_ago(dt: datetime) -> float:
    """Hours between a datetime and now."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now_utc() - dt
    return delta.total_seconds() / 3600


def days_ago(dt: datetime) -> float:
    return hours_ago(dt) / 24


def parse_iso(ts: str) -> datetime:
    """Parse an ISO timestamp string."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def safe_float(value, default=0.0) -> float:
    """Safely convert to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value, default=0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(value, max_val))


def score_range(value: float, min_val: float, max_val: float) -> float:
    """Map a value from [min_val, max_val] to [0, 10]. Clamped."""
    if max_val == min_val:
        return 5.0
    normalized = (value - min_val) / (max_val - min_val)
    return clamp(normalized * 10, 0, 10)


def ensure_data_dirs():
    """Create data directories if they don't exist."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.WEEKLY_REPORTS_DIR, exist_ok=True)
