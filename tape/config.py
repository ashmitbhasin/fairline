"""Endpoints, league definitions, paths and credentials (from the environment or a local .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# docs.kalshi.com/getting_started/api_environments (checked 2026-10-06)
KALSHI = {
    "prod": {
        "rest": "https://external-api.kalshi.com/trade-api/v2",
        "ws": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    },
    "demo": {
        "rest": "https://external-api.demo.kalshi.co/trade-api/v2",
        "ws": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    },
}
# The WebSocket handshake is signed as GET on this path, whatever the host.
WS_SIGN_PATH = "/trade-api/ws/v2"

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Core markets per league: game winner, spread ladder, total ladder.
LEAGUES: dict[str, dict] = {
    "nfl": {
        "kalshi_series": ["KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"],
        "odds_sport": "americanfootball_nfl",
    },
    "nba": {
        "kalshi_series": ["KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"],
        "odds_sport": "basketball_nba",
    },
}

# Up to 10 bookmakers bill as one region on The Odds API, so this list costs
# 3 credits per call for h2h + spreads + totals.
DEFAULT_BOOKMAKERS = [
    "pinnacle",
    "draftkings",
    "fanduel",
    "betmgm",
    "betrivers",
    "lowvig",
    "betonlineag",
    "bovada",
]


def data_dir() -> Path:
    load_dotenv()
    return Path(os.environ.get("FAIRLINE_DATA_DIR", ROOT / "data")).expanduser()


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from .env. Variables already set in the environment win."""
    try:
        text = path.read_text()
    except OSError:  # missing, or unreadable (macOS can refuse a background process a file on the Desktop)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class KalshiCreds:
    env: str
    key_id: str
    private_key_path: Path


def kalshi_creds(env: str) -> KalshiCreds | None:
    """Return the API key for `env` ("prod" or "demo"), or None if it isn't configured."""
    load_dotenv()
    prefix = f"KALSHI_{env.upper()}_"
    key_id = os.environ.get(prefix + "KEY_ID", "").strip()
    key_path = os.environ.get(prefix + "PRIVATE_KEY_PATH", "").strip()
    if not key_id or not key_path:
        return None
    path = Path(key_path).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return KalshiCreds(env=env, key_id=key_id, private_key_path=path)


def odds_api_key() -> str | None:
    load_dotenv()
    return os.environ.get("ODDS_API_KEY", "").strip() or None
