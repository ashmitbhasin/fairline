"""Kalshi team codes -> The Odds API team names.

Kalshi codes were read from live KXNFLGAME / KXNBAGAME markets on 2026-10-08; the NFL names match
The Odds API's americanfootball_nfl feed exactly. NBA names follow The Odds API's naming and are
checked at match time (an unmatched name only means no fair value is shown).
"""
from __future__ import annotations

NFL = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers", "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAC": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams", "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings", "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers", "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
NFL_ALIASES = {"JAX": "JAC", "LA": "LAR", "WSH": "WAS", "KAN": "KC", "GNB": "GB", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB"}

NBA = {
    "ATL": "Atlanta Hawks", "BKN": "Brooklyn Nets", "BOS": "Boston Celtics", "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}
NBA_ALIASES = {"BRK": "BKN", "CHO": "CHA", "GS": "GSW", "NO": "NOP", "NY": "NYK", "PHO": "PHX", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}

TABLES: dict[str, tuple[dict[str, str], dict[str, str]]] = {"nfl": (NFL, NFL_ALIASES), "nba": (NBA, NBA_ALIASES)}


def canonical(league: str, code: str | None) -> str | None:
    """Kalshi's code for a team, resolving common alternatives (JAX -> JAC)."""
    if not code:
        return None
    table, aliases = TABLES.get(league, ({}, {}))
    code = aliases.get(code.upper(), code.upper())
    return code if code in table else None


def full_name(league: str, code: str | None) -> str | None:
    code = canonical(league, code)
    return TABLES[league][0][code] if code else None


def short_name(league: str, code: str | None) -> str:
    """Team nickname for compact labels: DAL -> Cowboys. Falls back to the code."""
    name = full_name(league, code)
    if not name:
        return code or ""
    return "Trail Blazers" if name.endswith("Trail Blazers") else name.rsplit(" ", 1)[-1]


def split_game_code(league: str, code: str) -> tuple[str, str] | None:
    """'TBDAL' -> ('TB', 'DAL'): Kalshi writes the away team first, then the home team."""
    table = TABLES.get(league, ({}, {}))[0]
    for i in range(2, len(code) - 1):
        away, home = code[:i], code[i:]
        if away in table and home in table:
            return away, home
    return None
