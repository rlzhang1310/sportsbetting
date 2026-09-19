"""Interactive, fee-aware two-leg promo-bet odds finder.

The finder combines moneyline prices from The Odds API with executable Kalshi
order-book prices.  It deliberately keeps Kalshi's market ticker and YES/NO
side attached to every quote so a team can never be inferred from array order
or a ticker suffix.

Market data only: this program never places a bet or a Kalshi order.
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ODDS_API_ROOT = "https://api.the-odds-api.com/v4"
KALSHI_API_ROOT = "https://external-api.kalshi.com/trade-api/v2"
ZERO = Decimal("0")
ONE = Decimal("1")
SIMILAR_PRICE_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class SportSpec:
    label: str
    odds_key: str
    kalshi_series: str
    tie_possible: bool = False
    kalshi_spread_series: str | None = None
    kalshi_total_series: str | None = None


SPORTS: dict[str, SportSpec] = {
    "americanfootball_nfl": SportSpec(
        "NFL", "americanfootball_nfl", "KXNFLGAME", True,
        "KXNFLSPREAD", "KXNFLTOTAL"
    ),
    "americanfootball_ncaaf": SportSpec(
        "NCAAF", "americanfootball_ncaaf", "KXNCAAFGAME", False,
        "KXNCAAFSPREAD", "KXNCAAFTOTAL"
    ),
    "baseball_mlb": SportSpec(
        "MLB", "baseball_mlb", "KXMLBGAME", False,
        "KXMLBSPREAD", "KXMLBTOTAL"
    ),
    "basketball_nba": SportSpec(
        "NBA", "basketball_nba", "KXNBAGAME", False,
        "KXNBASPREAD", "KXNBATOTAL"
    ),
    "basketball_ncaab": SportSpec(
        "NCAAB", "basketball_ncaab", "KXNCAAMBGAME", False,
        "KXNCAAMBSPREAD", "KXNCAAMBTOTAL"
    ),
    "basketball_wnba": SportSpec(
        "WNBA", "basketball_wnba", "KXWNBAGAME", False,
        "KXWNBASPREAD", "KXWNBATOTAL"
    ),
    "icehockey_nhl": SportSpec(
        "NHL", "icehockey_nhl", "KXNHLGAME", False,
        "KXNHLSPREAD", "KXNHLTOTAL"
    ),
    "tennis_atp": SportSpec("ATP Tennis", "tennis_atp", "KXATPMATCH"),
    "tennis_wta": SportSpec("WTA Tennis", "tennis_wta", "KXWTAMATCH"),
}


NFL_ABBR_TO_FULL = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAC": "Jacksonville Jaguars",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LV": "Las Vegas Raiders",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}


class FinderError(RuntimeError):
    """A user-facing finder error."""


class ApiError(FinderError):
    """A sanitized upstream API error."""


def decimal_value(value: Any, *, field_name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinderError(f"Invalid decimal {field_name}: {value!r}") from exc
    if not result.is_finite():
        raise FinderError(f"Non-finite decimal {field_name}: {value!r}")
    return result


def ceil_to(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= ZERO:
        raise ValueError("Rounding increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def american_odds(decimal_odds: Decimal) -> str:
    """Convert decimal odds to conventional whole-number American odds."""

    if decimal_odds <= ONE:
        raise FinderError("Decimal odds must be greater than 1")
    raw = (
        Decimal("100") * (decimal_odds - ONE)
        if decimal_odds >= Decimal("2")
        else -Decimal("100") / (decimal_odds - ONE)
    )
    rounded = int(raw.quantize(ONE, rounding=ROUND_HALF_UP))
    return f"+{rounded}" if rounded >= 0 else str(rounded)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def load_env_file(path: str | Path = ".env") -> None:
    """Load a small dotenv subset without adding a package dependency.

    Existing environment variables win. Values may be unquoted, single-quoted,
    or double-quoted. This intentionally does not perform variable expansion.
    """

    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise FinderError(f"Could not read {env_path}: {exc}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def sanitized_url(url: str, secret_keys: Iterable[str] = ("apiKey",)) -> str:
    parts = urlsplit(url)
    secret_lower = {key.lower() for key in secret_keys}
    sanitized_pairs: list[tuple[str, str]] = []
    from urllib.parse import parse_qsl

    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        sanitized_pairs.append(
            (key, "REDACTED" if key.lower() in secret_lower else value)
        )
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(sanitized_pairs), "")
    )


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    headers: Mapping[str, str]


class OddsResponseCache:
    """Short-lived in-memory cache for an upstream market-data snapshot."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self._entries: dict[tuple[Any, ...], tuple[float, JsonResponse]] = {}
        self._lock = Lock()

    def get(self, key: tuple[Any, ...]) -> JsonResponse | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            created, response = entry
            if time.monotonic() - created > self.ttl_seconds:
                del self._entries[key]
                return None
            return response

    def put(self, key: tuple[Any, ...], response: JsonResponse) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), response)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class JsonHttpClient:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 3,
        user_agent: str = "sports-arbitrage-finder/1.0",
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.user_agent = user_agent

    def get(
        self,
        base_url: str,
        path: str = "",
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        secret_params: Iterable[str] = (),
    ) -> JsonResponse:
        query = urlencode(params or {}, doseq=True)
        url = base_url.rstrip("/") + "/" + path.lstrip("/") if path else base_url
        if query:
            url += ("&" if "?" in url else "?") + query
        safe_url = sanitized_url(url, secret_params)

        for attempt in range(self.retries + 1):
            request = Request(
                url,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                method="GET",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    try:
                        return JsonResponse(json.loads(payload), headers)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ApiError(
                            f"GET {safe_url} returned invalid JSON (HTTP {response.status})"
                        ) from exc
            except HTTPError as exc:
                body = exc.read(500).decode("utf-8", errors="replace").strip()
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < self.retries:
                        time.sleep(min(2.0, 0.25 * (2**attempt)))
                        continue
                detail = f": {body}" if body else ""
                raise ApiError(f"GET {safe_url} failed (HTTP {exc.code}){detail}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(min(2.0, 0.25 * (2**attempt)))
                    continue
                reason = getattr(exc, "reason", exc)
                raise ApiError(f"GET {safe_url} failed: {reason}") from exc
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class SportsbookOffer:
    selection: str
    bookmaker_key: str
    bookmaker_title: str
    decimal_odds: Decimal
    updated_at: datetime | None
    point: Decimal | None = None
    description: str | None = None


@dataclass
class OddsEvent:
    event_id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str
    offers: dict[str, list[SportsbookOffer]] = field(default_factory=dict)
    market_key: str = "h2h"
    market_label: str = "Moneyline"
    line: Decimal | None = None
    selections: tuple[str, str] | None = None
    base_event_id: str | None = None
    subject: str | None = None

    @property
    def outcomes(self) -> tuple[str, str]:
        return self.selections or (self.home_team, self.away_team)


class OddsApiClient:
    def __init__(
        self,
        http: JsonHttpClient,
        api_key: str,
        cache: OddsResponseCache | None = None,
    ) -> None:
        if not api_key:
            raise FinderError(
                "THE_ODDS_API_KEY is missing. Put it in .env or the environment."
            )
        self.http = http
        self.api_key = api_key
        self.cache = cache

    def _get(
        self,
        path: str,
        params: Mapping[str, Any],
    ) -> JsonResponse:
        cache_params = dict(params)
        commence_from = parse_timestamp(cache_params.pop("commenceTimeFrom", None))
        commence_to = parse_timestamp(cache_params.pop("commenceTimeTo", None))
        if commence_from is not None and commence_to is not None:
            cache_params["commenceWindowSeconds"] = int(
                (commence_to - commence_from).total_seconds()
            )
        key = (
            "odds",
            path,
            tuple(
                sorted(
                    (name, str(value)) for name, value in cache_params.items()
                )
            ),
        )
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return JsonResponse(
                    cached.data,
                    {**cached.headers, "x-snapshot-cache": "reused"},
                )
        response = self.http.get(
            ODDS_API_ROOT,
            path,
            params=params,
            secret_params=("apiKey",),
        )
        if self.cache is not None:
            self.cache.put(key, response)
        return response

    def get_sports(self) -> list[dict[str, Any]]:
        response = self._get(
            "sports", {"apiKey": self.api_key, "all": "true"}
        )
        if not isinstance(response.data, list):
            raise ApiError("The Odds API sports endpoint returned an unexpected shape")
        return [dict(item) for item in response.data if isinstance(item, Mapping)]

    def get_odds(
        self,
        sport_key: str,
        *,
        regions: Sequence[str],
        bookmakers: Sequence[str],
        markets: Sequence[str],
        commence_from: datetime | None,
        commence_to: datetime | None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        else:
            params["regions"] = ",".join(regions)
        if commence_from:
            params["commenceTimeFrom"] = iso_z(commence_from)
        if commence_to:
            params["commenceTimeTo"] = iso_z(commence_to)

        response = self._get(f"sports/{sport_key}/odds", params)
        if not isinstance(response.data, list):
            raise ApiError("The Odds API returned an unexpected response shape")
        quota = {
            key: response.headers[key]
            for key in (
                "x-requests-remaining",
                "x-requests-used",
                "x-requests-last",
                "x-snapshot-cache",
            )
            if key in response.headers
        }
        return response.data, quota

    def get_event_odds(
        self,
        sport_key: str,
        event_id: str,
        *,
        regions: Sequence[str],
        bookmakers: Sequence[str],
        markets: Sequence[str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        else:
            params["regions"] = ",".join(regions)
        response = self._get(
            f"sports/{sport_key}/events/{event_id}/odds", params
        )
        if not isinstance(response.data, Mapping):
            raise ApiError("The Odds API event endpoint returned an unexpected shape")
        quota = {
            key: response.headers[key]
            for key in (
                "x-requests-remaining",
                "x-requests-used",
                "x-requests-last",
                "x-snapshot-cache",
            )
            if key in response.headers
        }
        return dict(response.data), quota


def resolve_odds_sport_keys(
    client: OddsApiClient, sport: SportSpec
) -> tuple[str, ...]:
    """Resolve active tennis tournaments and merge active preseason feeds."""

    metadata = client.get_sports()
    active = {
        str(item.get("key", ""))
        for item in metadata
        if bool(item.get("active")) and item.get("key")
    }
    if sport.odds_key in {"tennis_atp", "tennis_wta"}:
        keys = sorted(
            key for key in active if key.startswith(f"{sport.odds_key}_")
        )
        if not keys:
            raise FinderError(f"No active {sport.label} tournaments are available")
        return tuple(keys)
    keys = [key for key in (sport.odds_key, f"{sport.odds_key}_preseason") if key in active]
    return tuple(keys or (sport.odds_key,))


def parse_odds_events(
    payload: Sequence[Mapping[str, Any]],
    *,
    max_quote_age: timedelta | None,
    now: datetime,
) -> tuple[list[OddsEvent], list[str]]:
    events: list[OddsEvent] = []
    diagnostics: list[str] = []
    for raw in payload:
        event_id = str(raw.get("id", "")).strip()
        home = str(raw.get("home_team", "")).strip()
        away = str(raw.get("away_team", "")).strip()
        commence = parse_timestamp(raw.get("commence_time"))
        if not event_id or not home or not away or commence is None:
            diagnostics.append("Skipped malformed Odds API event")
            continue
        allowed = {normalize_name(home): home, normalize_name(away): away}
        grouped: dict[tuple[str, Decimal | None], OddsEvent] = {}

        def comparison(
            market_key: str,
            line: Decimal | None,
            selections: tuple[str, str],
            label: str,
        ) -> OddsEvent:
            key = (market_key, line)
            if key not in grouped:
                suffix = "" if line is None else f":{decimal_text(line)}"
                grouped[key] = OddsEvent(
                    event_id=f"{event_id}:{market_key}{suffix}",
                    sport_key=str(raw.get("sport_key", "")),
                    commence_time=commence,
                    home_team=home,
                    away_team=away,
                    offers={name: [] for name in selections},
                    market_key=market_key,
                    market_label=label,
                    line=line,
                    selections=selections,
                    base_event_id=event_id,
                )
            return grouped[key]

        for book in raw.get("bookmakers", []) or []:
            if not isinstance(book, Mapping):
                continue
            book_key = str(book.get("key", "")).strip()
            book_title = str(book.get("title", book_key)).strip() or book_key
            updated = parse_timestamp(book.get("last_update"))
            if max_quote_age is not None:
                if updated is None or now - updated > max_quote_age:
                    continue
            for market in book.get("markets", []) or []:
                if not isinstance(market, Mapping):
                    continue
                market_key = str(market.get("key", ""))
                if market_key not in {"h2h", "spreads", "totals"}:
                    continue
                raw_outcomes = [
                    item
                    for item in (market.get("outcomes", []) or [])
                    if isinstance(item, Mapping)
                ]
                market_updated = parse_timestamp(market.get("last_update")) or updated
                if max_quote_age is not None:
                    if market_updated is None or now - market_updated > max_quote_age:
                        continue
                parsed_rows: list[tuple[str, Decimal, Decimal | None]] = []
                for outcome in raw_outcomes:
                    raw_name = str(outcome.get("name", "")).strip()
                    try:
                        odds = decimal_value(outcome.get("price"), field_name="decimal odds")
                    except FinderError:
                        continue
                    if odds <= ONE:
                        continue
                    point_value = outcome.get("point")
                    try:
                        point = (
                            decimal_value(point_value, field_name="market line")
                            if point_value is not None
                            else None
                        )
                    except FinderError:
                        continue
                    parsed_rows.append((raw_name, odds, point))
                if len(parsed_rows) != 2:
                    continue

                if market_key in {"h2h", "spreads"}:
                    by_team = {
                        allowed.get(normalize_name(name)): (odds, point)
                        for name, odds, point in parsed_rows
                    }
                    if set(by_team) != {home, away}:
                        continue
                    home_odds, home_point = by_team[home]
                    away_odds, away_point = by_team[away]
                    if market_key == "spreads":
                        if (
                            home_point is None
                            or away_point is None
                            or home_point + away_point != ZERO
                        ):
                            continue
                        line = home_point
                        label = f"Spread ({home} {home_point:+})"
                    else:
                        line = None
                        label = "Moneyline"
                    event = comparison(market_key, line, (home, away), label)
                    for selection, odds, point in (
                        (home, home_odds, home_point),
                        (away, away_odds, away_point),
                    ):
                        event.offers[selection].append(
                            SportsbookOffer(
                                selection=selection,
                                bookmaker_key=book_key,
                                bookmaker_title=book_title,
                                decimal_odds=odds,
                                updated_at=market_updated,
                                point=point,
                            )
                        )
                    continue

                by_side = {
                    name.casefold(): (odds, point) for name, odds, point in parsed_rows
                }
                if set(by_side) != {"over", "under"}:
                    continue
                over_odds, over_point = by_side["over"]
                under_odds, under_point = by_side["under"]
                if over_point is None or over_point != under_point:
                    continue
                over = f"Over {decimal_text(over_point)}"
                under = f"Under {decimal_text(over_point)}"
                event = comparison(
                    "totals", over_point, (over, under), f"Total {over_point}"
                )
                for selection, odds in ((over, over_odds), (under, under_odds)):
                    event.offers[selection].append(
                        SportsbookOffer(
                            selection=selection,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            decimal_odds=odds,
                            updated_at=market_updated,
                            point=over_point,
                        )
                    )
        usable = [event for event in grouped.values() if all(event.offers.values())]
        events.extend(usable)
        if not usable:
            diagnostics.append(f"No usable comparison quotes for {away} at {home}")
    return events, diagnostics


def parse_anytime_td_events(
    payload: Sequence[Mapping[str, Any]],
    *,
    max_quote_age: timedelta | None,
    now: datetime,
) -> list[OddsEvent]:
    """Turn player_anytime_td YES prices into one two-leg comparison per player."""

    events: list[OddsEvent] = []
    for raw in payload:
        base_id = str(raw.get("id", "")).strip()
        home = str(raw.get("home_team", "")).strip()
        away = str(raw.get("away_team", "")).strip()
        commence = parse_timestamp(raw.get("commence_time"))
        if not base_id or not home or not away or commence is None:
            continue
        players: dict[str, OddsEvent] = {}
        for book in raw.get("bookmakers", []) or []:
            if not isinstance(book, Mapping):
                continue
            book_key = str(book.get("key", "")).strip()
            book_title = str(book.get("title", book_key)).strip() or book_key
            updated = parse_timestamp(book.get("last_update"))
            for market in book.get("markets", []) or []:
                if not isinstance(market, Mapping) or market.get("key") != "player_anytime_td":
                    continue
                market_updated = parse_timestamp(market.get("last_update")) or updated
                if max_quote_age is not None and (
                    market_updated is None or now - market_updated > max_quote_age
                ):
                    continue
                for outcome in market.get("outcomes", []) or []:
                    if not isinstance(outcome, Mapping) or outcome.get("name") != "Yes":
                        continue
                    player = str(outcome.get("description", "")).strip()
                    try:
                        odds = decimal_value(outcome.get("price"), field_name="decimal odds")
                    except FinderError:
                        continue
                    if not player or odds <= ONE:
                        continue
                    identity = normalize_name(player)
                    yes = f"{player} scores a TD"
                    no = f"{player} does not score a TD"
                    event = players.setdefault(
                        identity,
                        OddsEvent(
                            event_id=f"{base_id}:player_anytime_td:{identity}",
                            sport_key=str(raw.get("sport_key", "")),
                            commence_time=commence,
                            home_team=home,
                            away_team=away,
                            offers={yes: [], no: []},
                            market_key="player_anytime_td",
                            market_label=f"Anytime touchdown - {player}",
                            selections=(yes, no),
                            base_event_id=base_id,
                            subject=player,
                        ),
                    )
                    event.offers[yes].append(
                        SportsbookOffer(
                            selection=yes,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            decimal_odds=odds,
                            updated_at=market_updated,
                            description=player,
                        )
                    )
        events.extend(players.values())
    return events


class KalshiApiClient:
    def __init__(
        self,
        http: JsonHttpClient,
        cache: OddsResponseCache | None = None,
    ) -> None:
        self.http = http
        self.cache = cache

    def _get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> JsonResponse:
        key = (
            "kalshi",
            path,
            tuple(
                sorted(
                    (name, str(value))
                    for name, value in (params or {}).items()
                )
            ),
        )
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        response = self.http.get(KALSHI_API_ROOT, path, params=params)
        if self.cache is not None:
            self.cache.put(key, response)
        return response

    def get_series(self, ticker: str) -> dict[str, Any]:
        data = self._get(f"series/{ticker}").data
        series = data.get("series") if isinstance(data, Mapping) else None
        if not isinstance(series, Mapping):
            raise ApiError(f"Kalshi series {ticker} returned an unexpected response")
        return dict(series)

    def get_open_events(self, series_ticker: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        for _ in range(100):
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = self._get("events", params).data
            if not isinstance(data, Mapping) or not isinstance(data.get("events"), list):
                raise ApiError("Kalshi events endpoint returned an unexpected response")
            events.extend(item for item in data["events"] if isinstance(item, dict))
            next_cursor = str(data.get("cursor") or "")
            if not next_cursor:
                return events
            if next_cursor in seen:
                raise ApiError("Kalshi events pagination repeated a cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ApiError("Kalshi events pagination exceeded 100 pages")

    def get_structured_targets(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        unique = sorted({item for item in ids if item})
        targets: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(unique), 2000):
            chunk = unique[offset : offset + 2000]
            if not chunk:
                continue
            data = self._get(
                "structured_targets",
                {"ids": chunk, "page_size": 2000},
            ).data
            rows = data.get("structured_targets") if isinstance(data, Mapping) else None
            if not isinstance(rows, list):
                raise ApiError(
                    "Kalshi structured_targets returned an unexpected response"
                )
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    targets[str(row["id"])] = row
        return targets

    def get_orderbook(self, market_ticker: str) -> dict[str, Any]:
        data = self._get(f"markets/{market_ticker}/orderbook").data
        if not isinstance(data, Mapping):
            raise ApiError(f"Unexpected orderbook response for {market_ticker}")
        return dict(data)

def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"\buniversity\b", "univ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def name_similarity(left: str, right: str) -> float:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    common = a_tokens & b_tokens
    if not common:
        return SequenceMatcher(None, a, b).ratio() * 0.65
    containment = len(common) / min(len(a_tokens), len(b_tokens))
    jaccard = len(common) / len(a_tokens | b_tokens)
    token_score = 0.84 * containment + 0.16 * jaccard
    sequence_score = SequenceMatcher(None, a, b).ratio()
    score = max(sequence_score, token_score)
    if len(common) == 1 and len(next(iter(common))) <= 2:
        score = min(score, 0.55)
    return score


def _structured_id(market: Mapping[str, Any]) -> str | None:
    custom = market.get("custom_strike")
    if not isinstance(custom, Mapping):
        return None
    for value in custom.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def collect_target_ids(events: Iterable[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for event in events:
        for market in event.get("markets", []) or []:
            if isinstance(market, Mapping):
                target_id = _structured_id(market)
                if target_id:
                    result.add(target_id)
    return result


def _participant_aliases(
    market: Mapping[str, Any], target: Mapping[str, Any] | None
) -> tuple[str, ...]:
    values: list[str] = []
    for value in (
        market.get("yes_sub_title"),
        market.get("subtitle"),
        market.get("primary_participant_key"),
    ):
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    if target:
        name = target.get("name")
        if isinstance(name, str) and name.strip():
            values.append(name.strip())
        details = target.get("details")
        if isinstance(details, Mapping):
            for key in ("market", "team_name", "abbreviation", "short_name"):
                value = details.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
            abbreviation = str(details.get("abbreviation") or "").upper()
            if abbreviation in NFL_ABBR_TO_FULL:
                values.append(NFL_ABBR_TO_FULL[abbreviation])
            team_name = details.get("team_name")
            if (
                isinstance(name, str)
                and isinstance(team_name, str)
                and team_name.strip()
            ):
                words = team_name.strip().split()
                if words and abbreviation and words[0].upper() == abbreviation:
                    words = words[1:]
                if words:
                    values.append(f"{name.strip()} {' '.join(words)}")
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(value)
    return tuple(unique)


def parse_tie_settlement(market: Mapping[str, Any]) -> Decimal | None:
    rules = " ".join(
        str(market.get(key) or "") for key in ("rules_primary", "rules_secondary")
    )
    if not re.search(r"\btie\b", rules, flags=re.IGNORECASE):
        return None
    if re.search(
        r"\btie\b[\s\S]{0,220}(?:\$\s*0\.50|\b0\.5(?:0+)?\b|\b50\s*cents?\b)",
        rules,
        flags=re.IGNORECASE,
    ):
        return Decimal("0.5")
    return None


@dataclass
class KalshiParticipant:
    identity: str
    label: str
    aliases: tuple[str, ...]
    market: dict[str, Any]
    tie_yes_settlement: Decimal | None


@dataclass
class KalshiEvent:
    event_ticker: str
    series_ticker: str
    title: str
    occurrence_time: datetime
    mutually_exclusive: bool
    participants: tuple[KalshiParticipant, ...]
    series_title: str = ""
    fee_type_override: str | None = None
    fee_multiplier_override: Decimal | None = None


def parse_kalshi_events(
    payload: Sequence[Mapping[str, Any]],
    targets: Mapping[str, Mapping[str, Any]],
    *,
    series_title: str = "",
) -> tuple[list[KalshiEvent], list[str]]:
    parsed: list[KalshiEvent] = []
    diagnostics: list[str] = []
    for raw_event in payload:
        ticker = str(raw_event.get("event_ticker", "")).strip()
        markets = raw_event.get("markets", []) or []
        participants: list[KalshiParticipant] = []
        times: list[datetime] = []
        identities: set[str] = set()
        for raw_market in markets:
            if not isinstance(raw_market, Mapping):
                continue
            status = str(raw_market.get("status", "")).casefold()
            # Nested markets currently use the lifecycle value ``active``.
            # Do not treat a missing or legacy-looking value as tradeable.
            if status != "active":
                continue
            if raw_market.get("market_type") not in (None, "binary"):
                continue
            market_ticker = str(raw_market.get("ticker", "")).strip()
            yes_label = str(raw_market.get("yes_sub_title", "")).strip()
            if not market_ticker or not yes_label:
                continue
            target_id = _structured_id(raw_market)
            target = targets.get(target_id or "")
            identity = target_id or normalize_name(yes_label)
            if not identity or identity in identities:
                diagnostics.append(
                    f"{ticker or 'unknown Kalshi event'} has a duplicate participant; "
                    "ignored the duplicate market"
                )
                continue
            identities.add(identity)
            aliases = _participant_aliases(raw_market, target)
            if not aliases:
                continue
            target_name = target.get("name") if target else None
            label = (
                target_name.strip()
                if isinstance(target_name, str) and target_name.strip()
                else yes_label
            )
            participant_time = parse_timestamp(raw_market.get("occurrence_datetime"))
            if participant_time:
                times.append(participant_time)
            participants.append(
                KalshiParticipant(
                    identity=identity,
                    label=label,
                    aliases=aliases,
                    market=dict(raw_market),
                    tie_yes_settlement=parse_tie_settlement(raw_market),
                )
            )
        if not ticker or not participants or not times:
            diagnostics.append(f"Skipped incomplete Kalshi event {ticker or '<unknown>'}")
            continue
        earliest = min(times)
        if max(times) - earliest > timedelta(minutes=15):
            diagnostics.append(
                f"Skipped {ticker}: participant occurrence times disagree"
            )
            continue
        fee_type_value = raw_event.get("fee_type_override")
        fee_type_override = (
            str(fee_type_value).strip() if fee_type_value not in (None, "") else None
        )
        fee_multiplier_value = raw_event.get("fee_multiplier_override")
        try:
            fee_multiplier_override = (
                decimal_value(
                    fee_multiplier_value, field_name="Kalshi event fee multiplier"
                )
                if fee_multiplier_value is not None
                else None
            )
        except FinderError as exc:
            diagnostics.append(f"Skipped {ticker}: {exc}")
            continue
        if fee_multiplier_override is not None and fee_multiplier_override < ZERO:
            diagnostics.append(f"Skipped {ticker}: negative event fee multiplier")
            continue
        parsed.append(
            KalshiEvent(
                event_ticker=ticker,
                series_ticker=str(raw_event.get("series_ticker", "")),
                title=str(raw_event.get("title", ticker)),
                occurrence_time=earliest,
                mutually_exclusive=bool(raw_event.get("mutually_exclusive")),
                participants=tuple(participants),
                series_title=series_title,
                fee_type_override=fee_type_override,
                fee_multiplier_override=fee_multiplier_override,
            )
        )
    return parsed, diagnostics


@dataclass(frozen=True)
class ParticipantAssignment:
    mapping: Mapping[str, str]
    average_score: float
    minimum_score: float
    margin: float


def assign_participants(
    participants: Sequence[KalshiParticipant], outcomes: Sequence[str]
) -> ParticipantAssignment | None:
    if len(participants) != len(outcomes) or not participants:
        return None
    candidates: list[tuple[float, float, tuple[str, ...]]] = []
    for permutation in itertools.permutations(outcomes):
        scores = [
            max(name_similarity(alias, outcome) for alias in participant.aliases)
            for participant, outcome in zip(participants, permutation)
        ]
        candidates.append((sum(scores) / len(scores), min(scores), permutation))
    candidates.sort(key=lambda row: (row[1], row[0]), reverse=True)
    average, minimum, permutation = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
    return ParticipantAssignment(
        mapping={
            participant.identity: outcome
            for participant, outcome in zip(participants, permutation)
        },
        average_score=average,
        minimum_score=minimum,
        margin=average - runner_up,
    )


@dataclass(frozen=True)
class MatchedEvent:
    odds: OddsEvent
    kalshi: KalshiEvent
    participant_to_selection: Mapping[str, str]
    team_score: float
    time_delta: timedelta


def match_events(
    odds_events: Sequence[OddsEvent],
    kalshi_events: Sequence[KalshiEvent],
    *,
    tolerance: timedelta,
    minimum_team_score: float = 0.62,
    minimum_assignment_margin: float = 0.06,
) -> tuple[list[MatchedEvent], list[str]]:
    candidates: list[
        tuple[float, float, float, int, int, ParticipantAssignment, timedelta]
    ] = []
    diagnostics: list[str] = []
    for odds_index, odds in enumerate(odds_events):
        for kalshi_index, kalshi in enumerate(kalshi_events):
            delta = abs(odds.commence_time - kalshi.occurrence_time)
            if delta > tolerance:
                continue
            assignment = assign_participants(kalshi.participants, odds.outcomes)
            if assignment is None:
                continue
            if assignment.minimum_score < minimum_team_score:
                continue
            if assignment.margin < minimum_assignment_margin:
                continue
            time_fraction = delta.total_seconds() / max(tolerance.total_seconds(), 1)
            combined = assignment.average_score - 0.10 * time_fraction
            candidates.append(
                (
                    combined,
                    assignment.minimum_score,
                    -delta.total_seconds(),
                    odds_index,
                    kalshi_index,
                    assignment,
                    delta,
                )
            )
    candidates.sort(key=lambda row: row[:3], reverse=True)
    used_odds: set[int] = set()
    used_kalshi: set[int] = set()
    matches: list[MatchedEvent] = []
    for _, _, _, odds_index, kalshi_index, assignment, delta in candidates:
        if odds_index in used_odds or kalshi_index in used_kalshi:
            continue
        used_odds.add(odds_index)
        used_kalshi.add(kalshi_index)
        matches.append(
            MatchedEvent(
                odds=odds_events[odds_index],
                kalshi=kalshi_events[kalshi_index],
                participant_to_selection=assignment.mapping,
                team_score=assignment.average_score,
                time_delta=delta,
            )
        )
    matches.sort(key=lambda item: item.odds.commence_time)
    for index, event in enumerate(odds_events):
        if index not in used_odds:
            diagnostics.append(
                f"Unmatched Odds event: {event.away_team} at {event.home_team} "
                f"({iso_z(event.commence_time)})"
            )
    return matches, diagnostics


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class ParsedOrderbook:
    yes_bids: tuple[PriceLevel, ...]
    no_bids: tuple[PriceLevel, ...]


def _parse_levels(rows: Any, *, cents: bool = False) -> tuple[PriceLevel, ...]:
    levels: list[PriceLevel] = []
    if not isinstance(rows, list):
        return ()
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = decimal_value(row[0], field_name="orderbook price")
            quantity = decimal_value(row[1], field_name="orderbook quantity")
        except FinderError:
            continue
        if cents:
            price /= Decimal("100")
        if ZERO < price < ONE and quantity > ZERO:
            levels.append(PriceLevel(price, quantity))
    levels.sort(key=lambda level: level.price)
    return tuple(levels)


def parse_orderbook(payload: Mapping[str, Any]) -> ParsedOrderbook:
    current = payload.get("orderbook_fp")
    if isinstance(current, Mapping):
        return ParsedOrderbook(
            yes_bids=_parse_levels(current.get("yes_dollars")),
            no_bids=_parse_levels(current.get("no_dollars")),
        )
    legacy = payload.get("orderbook")
    if isinstance(legacy, Mapping):
        return ParsedOrderbook(
            yes_bids=_parse_levels(legacy.get("yes"), cents=True),
            no_bids=_parse_levels(legacy.get("no"), cents=True),
        )
    raise FinderError("Kalshi orderbook has neither orderbook_fp nor orderbook")


def asks_for_side(orderbook: ParsedOrderbook, side: str) -> tuple[PriceLevel, ...]:
    normalized = side.casefold()
    if normalized == "yes":
        bids = orderbook.no_bids
    elif normalized == "no":
        bids = orderbook.yes_bids
    else:
        raise ValueError(f"Unknown Kalshi side {side!r}")
    asks = [PriceLevel(ONE - level.price, level.quantity) for level in bids]
    asks.sort(key=lambda level: level.price)
    return tuple(asks)


def bids_for_side(orderbook: ParsedOrderbook, side: str) -> tuple[PriceLevel, ...]:
    normalized = side.casefold()
    if normalized == "yes":
        bids = orderbook.yes_bids
    elif normalized == "no":
        bids = orderbook.no_bids
    else:
        raise ValueError(f"Unknown Kalshi side {side!r}")
    return tuple(sorted(bids, key=lambda level: level.price, reverse=True))


@dataclass(frozen=True)
class FilledLevel:
    price: Decimal
    quantity: Decimal


def fill_levels(
    levels: Sequence[PriceLevel], quantity: Decimal
) -> tuple[tuple[FilledLevel, ...], Decimal] | None:
    remaining = quantity
    fills: list[FilledLevel] = []
    position_cost = ZERO
    for level in sorted(levels, key=lambda item: item.price):
        if remaining <= ZERO:
            break
        used = min(level.quantity, remaining)
        if used <= ZERO:
            continue
        fills.append(FilledLevel(level.price, used))
        position_cost += level.price * used
        remaining -= used
    if remaining > ZERO:
        return None
    return tuple(fills), position_cost


@dataclass(frozen=True)
class FeeModel:
    fee_type: str
    multiplier: Decimal

    def _quadratic_fee(
        self,
        fills: Sequence[FilledLevel],
        rate: Decimal,
    ) -> Decimal:
        if self.multiplier == ZERO:
            return ZERO
        if not self.fee_type.startswith("quadratic"):
            raise FinderError(f"Unsupported Kalshi fee type: {self.fee_type!r}")
        total = ZERO
        for fill in fills:
            raw = (
                self.multiplier
                * rate
                * fill.quantity
                * fill.price
                * (ONE - fill.price)
            )
            total += ceil_to(raw, Decimal("0.000001"))
        return total

    def taker_fee(self, fills: Sequence[FilledLevel]) -> Decimal:
        return self._quadratic_fee(fills, Decimal("0.07"))

    def limit_fee(self, fills: Sequence[FilledLevel]) -> Decimal:
        """Fee for a resting limit order, at one quarter of the taker rate."""

        return self._quadratic_fee(fills, Decimal("0.0175"))


def series_fee_model(series: Mapping[str, Any]) -> FeeModel:
    fee_type = str(series.get("fee_type") or "").strip()
    multiplier = decimal_value(
        series.get("fee_multiplier", 0), field_name="Kalshi fee multiplier"
    )
    if multiplier < ZERO:
        raise FinderError("Kalshi returned a negative fee multiplier")
    if multiplier and not fee_type:
        raise FinderError("Kalshi returned a fee multiplier without a fee type")
    return FeeModel(fee_type=fee_type or "quadratic", multiplier=multiplier)


def current_event_fee_model(base: FeeModel, event: KalshiEvent) -> FeeModel:
    """Apply the current override embedded in Kalshi's Event response."""

    if event.fee_type_override is None and event.fee_multiplier_override is None:
        return base
    return FeeModel(
        fee_type=event.fee_type_override or base.fee_type,
        multiplier=(
            event.fee_multiplier_override
            if event.fee_multiplier_override is not None
            else base.multiplier
        ),
    )


def raw_event_fee_model(base: FeeModel, event: Mapping[str, Any]) -> FeeModel:
    fee_type = event.get("fee_type_override")
    multiplier = event.get("fee_multiplier_override")
    return FeeModel(
        fee_type=str(fee_type or base.fee_type),
        multiplier=(
            decimal_value(multiplier, field_name="event fee multiplier")
            if multiplier is not None
            else base.multiplier
        ),
    )


@dataclass(frozen=True)
class KalshiRoute:
    selection: str
    market_ticker: str
    side: str
    asks: tuple[PriceLevel, ...]
    notional: Decimal
    fractional: bool
    tie_settlement: Decimal | None
    fee_model: FeeModel
    bids: tuple[PriceLevel, ...] = ()


@dataclass(frozen=True)
class KalshiRouteSpec:
    selection: str
    market_ticker: str
    side: str
    notional: Decimal
    fractional: bool
    fee_model: FeeModel


def _event_occurrence(raw_event: Mapping[str, Any]) -> datetime | None:
    times = [
        parse_timestamp(market.get("occurrence_datetime"))
        for market in raw_event.get("markets", []) or []
        if isinstance(market, Mapping)
    ]
    return min((value for value in times if value is not None), default=None)


def _event_title_tokens(title: Any) -> set[str]:
    text = normalize_name(str(title).split(":", 1)[0])
    ignored = {
        "at", "vs", "versus", "winner", "game", "match", "spread",
        "total", "points", "runs", "goals", "pro", "professional",
        "football", "basketball", "baseball", "hockey",
    }
    return {token for token in text.split() if token not in ignored}


def match_derivative_event(
    base_event: KalshiEvent,
    raw_events: Sequence[Mapping[str, Any]],
    *,
    tolerance: timedelta,
) -> Mapping[str, Any] | None:
    """Match Kalshi series by title participants and UTC occurrence time."""

    base_tokens = _event_title_tokens(base_event.title)
    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for raw in raw_events:
        occurrence = _event_occurrence(raw)
        if occurrence is None or abs(occurrence - base_event.occurrence_time) > tolerance:
            continue
        tokens = _event_title_tokens(raw.get("title", ""))
        if not tokens or not base_tokens:
            continue
        score = len(tokens & base_tokens) / len(tokens | base_tokens)
        if score >= 0.60:
            candidates.append((score, raw))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.05:
        return None
    return candidates[0][1]


def derivative_route_specs(
    comparison: OddsEvent,
    base_match: MatchedEvent,
    raw_event: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
    fee_model: FeeModel,
) -> list[KalshiRouteSpec]:
    specs: list[KalshiRouteSpec] = []
    for market in raw_event.get("markets", []) or []:
        if not isinstance(market, Mapping) or str(market.get("status", "")).casefold() != "active":
            continue
        ticker = str(market.get("ticker", "")).strip()
        try:
            floor = decimal_value(market.get("floor_strike"), field_name="strike")
            notional = decimal_value(
                market.get("notional_value_dollars", "1"), field_name="notional"
            )
        except FinderError:
            continue
        if not ticker or notional <= ZERO:
            continue
        fractional = bool(market.get("fractional_trading_enabled", False))
        if comparison.market_key == "totals":
            if comparison.line != floor:
                continue
            specs.extend(
                (
                    KalshiRouteSpec(
                        comparison.outcomes[0], ticker, "yes", notional,
                        fractional, fee_model
                    ),
                    KalshiRouteSpec(
                        comparison.outcomes[1], ticker, "no", notional,
                        fractional, fee_model
                    ),
                )
            )
            break
        if comparison.market_key == "spreads":
            custom = market.get("custom_strike")
            ids = set(custom.values()) if isinstance(custom, Mapping) else set()
            identity = next(
                (item for item in ids if item in base_match.participant_to_selection),
                None,
            )
            if identity is None or comparison.line is None:
                continue
            team = base_match.participant_to_selection[str(identity)]
            team_point = (
                comparison.line
                if team == comparison.home_team
                else -comparison.line
            )
            if -team_point != floor:
                continue
            opponent = (
                comparison.away_team
                if team == comparison.home_team
                else comparison.home_team
            )
            specs.extend(
                (
                    KalshiRouteSpec(
                        team, ticker, "yes", notional, fractional, fee_model
                    ),
                    KalshiRouteSpec(
                        opponent, ticker, "no", notional, fractional, fee_model
                    ),
                )
            )
            break
        if comparison.market_key == "player_anytime_td":
            if floor != Decimal("0.5") or not comparison.subject:
                continue
            custom = market.get("custom_strike")
            player_id = None
            if isinstance(custom, Mapping):
                player_id = next(
                    (
                        str(value)
                        for key, value in custom.items()
                        if "player" in str(key).casefold() and value
                    ),
                    None,
                )
            target = targets.get(player_id or "")
            aliases = _participant_aliases(market, target)
            if max(
                (name_similarity(comparison.subject, alias) for alias in aliases),
                default=0.0,
            ) < 0.82:
                continue
            # The sportsbook feed supplies YES-only anytime scorer prices. BUY
            # NO on this exact player proposition is the opposing hedge leg.
            specs.append(
                KalshiRouteSpec(
                    comparison.outcomes[1], ticker, "no", notional,
                    fractional, fee_model
                )
            )
            break
    return specs


def materialize_route(
    spec: KalshiRouteSpec, books: Mapping[str, ParsedOrderbook]
) -> KalshiRoute | None:
    book = books.get(spec.market_ticker)
    if book is None:
        return None
    return KalshiRoute(
        selection=spec.selection,
        market_ticker=spec.market_ticker,
        side=spec.side,
        asks=asks_for_side(book, spec.side),
        notional=spec.notional,
        fractional=spec.fractional,
        tie_settlement=None,
        fee_model=spec.fee_model,
        bids=bids_for_side(book, spec.side),
    )


@dataclass(frozen=True)
class EvaluatedQuote:
    selection: str
    source_type: str
    venue: str
    cost: Decimal
    win_return: Decimal
    tie_return: Decimal | None
    detail: Mapping[str, Any]


def evaluate_sportsbook_offer(
    offer: SportsbookOffer,
    *,
    target_payout: Decimal,
    stake_increment: Decimal,
    tie_mode: str,
) -> EvaluatedQuote:
    stake = ceil_to(target_payout / offer.decimal_odds, stake_increment)
    tie_return = stake if tie_mode == "push" else ZERO
    return EvaluatedQuote(
        selection=offer.selection,
        source_type="sportsbook",
        venue=offer.bookmaker_title,
        cost=stake,
        win_return=stake * offer.decimal_odds,
        tie_return=tie_return,
        detail={
            "bookmaker_key": offer.bookmaker_key,
            "decimal_odds": decimal_text(offer.decimal_odds),
            "american_odds": american_odds(offer.decimal_odds),
            "stake": decimal_text(stake),
            "stake_increment": decimal_text(stake_increment),
            "point": decimal_text(offer.point) if offer.point is not None else None,
            "description": offer.description,
            "updated_at": iso_z(offer.updated_at) if offer.updated_at else None,
        },
    )


def attach_similar_sportsbooks(
    quotes: Sequence[EvaluatedQuote],
    *,
    tolerance: Decimal = SIMILAR_PRICE_TOLERANCE,
) -> list[EvaluatedQuote]:
    """Attach nearby sportsbook prices to every possible selected quote."""

    sportsbook_quotes = [
        quote for quote in quotes if quote.source_type == "sportsbook"
    ]
    enriched: list[EvaluatedQuote] = []
    for quote in quotes:
        target_probability = quote.cost / quote.win_return
        alternatives: dict[str, dict[str, str]] = {}
        for other in sportsbook_quotes:
            if (
                quote.source_type == "sportsbook"
                and other.detail.get("bookmaker_key")
                == quote.detail.get("bookmaker_key")
            ):
                continue
            implied = other.cost / other.win_return
            difference = abs(implied - target_probability)
            if difference > tolerance:
                continue
            key = str(other.detail.get("bookmaker_key", other.venue))
            item = {
                "venue": other.venue,
                "bookmaker_key": key,
                "american_odds": str(other.detail.get("american_odds", "")),
                "decimal_odds": str(other.detail.get("decimal_odds", "")),
                "implied_probability": decimal_text(implied),
                "difference": decimal_text(difference),
            }
            previous = alternatives.get(key)
            if previous is None or decimal_value(
                item["implied_probability"]
            ) < decimal_value(previous["implied_probability"]):
                alternatives[key] = item
        detail = dict(quote.detail)
        detail["similar_sportsbooks"] = sorted(
            alternatives.values(),
            key=lambda item: decimal_value(item["implied_probability"]),
        )
        enriched.append(
            EvaluatedQuote(
                selection=quote.selection,
                source_type=quote.source_type,
                venue=quote.venue,
                cost=quote.cost,
                win_return=quote.win_return,
                tie_return=quote.tie_return,
                detail=detail,
            )
        )
    return enriched


def evaluate_kalshi_route(
    route: KalshiRoute,
    *,
    target_payout: Decimal,
    balance_precision: Decimal,
) -> EvaluatedQuote | None:
    contract_increment = Decimal("0.01") if route.fractional else ONE
    contracts = ceil_to(target_payout / route.notional, contract_increment)
    filled = fill_levels(route.asks, contracts)
    if filled is None:
        return None
    fills, position_cost = filled
    model_fee = route.fee_model.taker_fee(fills)
    total_cost = ceil_to(position_cost + model_fee, balance_precision)
    average_price = position_cost / contracts
    current_level = route.asks[0]
    one_cent_lower_bid = max(
        ZERO, current_level.price - Decimal("0.01")
    )
    current_unit_fee = route.fee_model.taker_fee(
        (FilledLevel(current_level.price, ONE),)
    )
    lower_bid_unit_fee = route.fee_model.limit_fee(
        (FilledLevel(one_cent_lower_bid, ONE),)
    )
    current_implied_probability = (
        current_level.price + current_unit_fee
    ) / route.notional
    lower_bid_implied_probability = (
        one_cent_lower_bid + lower_bid_unit_fee
    ) / route.notional
    bid_volume = sum(
        (
            level.quantity
            for level in route.bids
            if level.price == one_cent_lower_bid
        ),
        ZERO,
    )
    tie_return = (
        contracts * route.notional * route.tie_settlement
        if route.tie_settlement is not None
        else None
    )
    return EvaluatedQuote(
        selection=route.selection,
        source_type="kalshi",
        venue="Kalshi",
        cost=total_cost,
        win_return=contracts * route.notional,
        tie_return=tie_return,
        detail={
            "market_ticker": route.market_ticker,
            "side": route.side.upper(),
            "contracts": decimal_text(contracts),
            "average_price": decimal_text(
                average_price.quantize(Decimal("0.0001"))
            ),
            "current_price": decimal_text(current_level.price),
            "current_price_volume": decimal_text(current_level.quantity),
            "current_price_implied_probability": decimal_text(
                current_implied_probability
            ),
            "one_cent_lower_bid_price": decimal_text(one_cent_lower_bid),
            "one_cent_lower_bid_volume": decimal_text(bid_volume),
            "one_cent_lower_bid_implied_probability": decimal_text(
                lower_bid_implied_probability
            ),
            "position_cost": decimal_text(position_cost),
            "fee": decimal_text(total_cost - position_cost),
            "fee_type": route.fee_model.fee_type,
            "fee_multiplier": decimal_text(route.fee_model.multiplier),
            "maker_fee_rate": "0.0175",
            "notional": decimal_text(route.notional),
            "fractional": route.fractional,
            "balance_precision": decimal_text(balance_precision),
            "tie_settlement": (
                decimal_text(route.tie_settlement)
                if route.tie_settlement is not None
                else None
            ),
            "orderbook_levels": [
                {
                    "price": decimal_text(level.price),
                    "quantity": decimal_text(level.quantity),
                }
                for level in route.asks
            ],
            "levels": [
                {
                    "price": decimal_text(fill.price),
                    "quantity": decimal_text(fill.quantity),
                }
                for fill in fills
            ],
        },
    )


def make_kalshi_routes(
    match: MatchedEvent,
    orderbooks: Mapping[str, ParsedOrderbook],
    fee_model: FeeModel,
    *,
    tie_possible: bool,
) -> tuple[list[KalshiRoute], list[str]]:
    routes: list[KalshiRoute] = []
    diagnostics: list[str] = []
    participants = match.kalshi.participants
    if len(participants) != 2 or not match.kalshi.mutually_exclusive:
        return [], [
            f"Skipped Kalshi routes for {match.kalshi.event_ticker}: expected "
            "exactly two mutually-exclusive participants"
        ]
    selections = [match.participant_to_selection[p.identity] for p in participants]
    for index, participant in enumerate(participants):
        market = participant.market
        ticker = str(market.get("ticker", ""))
        orderbook = orderbooks.get(ticker)
        if orderbook is None:
            continue
        if tie_possible and participant.tie_yes_settlement is None:
            diagnostics.append(
                f"Skipped {ticker}: its tie settlement could not be verified"
            )
            continue
        notional = decimal_value(
            market.get("notional_value_dollars", "1"),
            field_name="Kalshi notional",
        )
        if notional <= ZERO:
            continue
        fractional = bool(market.get("fractional_trading_enabled", False))
        own_selection = selections[index]
        yes_tie = participant.tie_yes_settlement if tie_possible else None
        routes.append(
            KalshiRoute(
                selection=own_selection,
                market_ticker=ticker,
                side="yes",
                asks=asks_for_side(orderbook, "yes"),
                notional=notional,
                fractional=fractional,
                tie_settlement=yes_tie,
                fee_model=fee_model,
                bids=bids_for_side(orderbook, "yes"),
            )
        )
        # Deliberately do not relabel BUY NO on one team as BUY YES on the
        # opponent. Mutually exclusive does not necessarily mean exhaustive,
        # and Kalshi exposes a direct YES market for each matched participant.
    return routes, diagnostics


@dataclass(frozen=True)
class Candidate:
    match: MatchedEvent
    legs: tuple[EvaluatedQuote, ...]
    total_cost: Decimal
    scenario_profits: Mapping[str, Decimal]
    win_profit: Decimal
    win_roi: Decimal
    worst_case_profit: Decimal
    uses_kalshi: bool
    implied_probabilities: Mapping[str, Decimal]
    implied_probability_sum: Decimal
    vig: Decimal
    vig_gap: Decimal
    low_probability: Decimal
    probability_spread: Decimal
    eligible: bool
    rejection_reason: str | None


def candidate_sort_key(candidate: Candidate) -> tuple[Decimal, Decimal]:
    """Order results by signed vig, with the most negative vig first."""

    return candidate.vig, candidate.low_probability


def build_candidate(
    match: MatchedEvent,
    kalshi_routes: Sequence[KalshiRoute],
    *,
    target_payout: Decimal,
    wager_amount: Decimal | None = None,
    stake_increment: Decimal,
    balance_precision: Decimal,
    tie_possible: bool,
    sportsbook_tie_mode: str,
    minimum_profit: Decimal,
    minimum_roi: Decimal,
    require_kalshi: bool,
    maximum_vig: Decimal | None = None,
    max_low_probability: Decimal | None = None,
) -> Candidate | None:
    evaluated: dict[str, list[EvaluatedQuote]] = {
        selection: [] for selection in match.odds.outcomes
    }
    for selection, offers in match.odds.offers.items():
        for offer in offers:
            evaluated[selection].append(
                evaluate_sportsbook_offer(
                    offer,
                    target_payout=target_payout,
                    stake_increment=stake_increment,
                    tie_mode=sportsbook_tie_mode,
                )
            )
    for route in kalshi_routes:
        try:
            quote = evaluate_kalshi_route(
                route,
                target_payout=target_payout,
                balance_precision=balance_precision,
            )
        except FinderError:
            # Unknown fee types cannot be priced safely. Sportsbook quotes for
            # the event remain usable.
            continue
        if quote is not None and quote.selection in evaluated:
            evaluated[quote.selection].append(quote)
    for selection in evaluated:
        evaluated[selection] = attach_similar_sportsbooks(evaluated[selection])
    if any(not evaluated[selection] for selection in match.odds.outcomes):
        return None
    combinations: list[Candidate] = []
    outcome_quotes = [evaluated[name] for name in match.odds.outcomes]
    for raw_legs in itertools.product(*outcome_quotes):
        legs = tuple(raw_legs)
        total_cost = sum((leg.cost for leg in legs), ZERO)
        scenario_profits = {
            selection: sum(
                (
                    leg.win_return if leg.selection == selection else ZERO
                    for leg in legs
                ),
                ZERO,
            )
            - total_cost
            for selection in match.odds.outcomes
        }
        unknown_tie = False
        if tie_possible:
            tie_returns: list[Decimal] = []
            for leg in legs:
                if leg.tie_return is None:
                    unknown_tie = True
                    break
                tie_returns.append(leg.tie_return)
            if not unknown_tie:
                scenario_profits["Tie"] = sum(tie_returns, ZERO) - total_cost
        win_profit = min(scenario_profits[name] for name in match.odds.outcomes)
        win_roi = win_profit / total_cost if total_cost else ZERO
        worst_case = min(scenario_profits.values())
        uses_kalshi = any(leg.source_type == "kalshi" for leg in legs)
        implied = {
            leg.selection: leg.cost / leg.win_return for leg in legs
        }
        implied_sum = sum(implied.values(), ZERO)
        vig = implied_sum - ONE
        vig_gap = abs(vig)
        low_probability = min(implied.values())
        probability_spread = max(implied.values()) - low_probability

        reason: str | None = None
        if unknown_tie:
            reason = "tie settlement is unknown"
        elif tie_possible and scenario_profits.get("Tie", ZERO) < ZERO:
            reason = "negative modeled tie payoff"
        elif require_kalshi and not uses_kalshi:
            reason = "combination does not use Kalshi"
        elif maximum_vig is not None and vig_gap > maximum_vig:
            reason = "vig gap is above threshold"
        elif (
            max_low_probability is not None
            and low_probability > max_low_probability
        ):
            reason = "lower-probability leg is above threshold"
        elif minimum_profit > ZERO and win_profit < minimum_profit:
            reason = "profit is below threshold"
        elif minimum_roi > ZERO and win_roi < minimum_roi:
            reason = "ROI is below threshold"
        combinations.append(
            Candidate(
                match=match,
                legs=legs,
                total_cost=total_cost,
                scenario_profits=scenario_profits,
                win_profit=win_profit,
                win_roi=win_roi,
                worst_case_profit=worst_case,
                uses_kalshi=uses_kalshi,
                implied_probabilities=implied,
                implied_probability_sum=implied_sum,
                vig=vig,
                vig_gap=vig_gap,
                low_probability=low_probability,
                probability_spread=probability_spread,
                eligible=reason is None,
                rejection_reason=reason,
            )
        )
    if not combinations:
        return None
    best = min(
        combinations,
        key=lambda item: (
            not item.eligible,
            item.vig_gap,
            item.low_probability,
            not item.uses_kalshi,
        ),
    )
    if wager_amount is None:
        return best

    # In the browser UI the sizing input represents the cash placed on the
    # sportsbook leg, not an arbitrary gross payout. Size every opposing leg
    # to that sportsbook leg's gross return. For example, $100 at +900 has a
    # $1,000 gross return, so a standard $1 Kalshi market needs 1,000
    # contracts. The lowest-implied-probability sportsbook leg is the natural
    # promotional wager when both legs happen to be sportsbooks.
    sportsbook_legs = [
        leg for leg in best.legs if leg.source_type == "sportsbook"
    ]
    if not sportsbook_legs:
        return best
    wager_leg = min(
        sportsbook_legs,
        key=lambda leg: leg.cost / leg.win_return,
    )
    decimal_odds = decimal_value(
        wager_leg.detail["decimal_odds"], field_name="sportsbook decimal odds"
    )
    wager_target_payout = wager_amount * decimal_odds
    return build_candidate(
        match,
        kalshi_routes,
        target_payout=wager_target_payout,
        stake_increment=stake_increment,
        balance_precision=balance_precision,
        tie_possible=tie_possible,
        sportsbook_tie_mode=sportsbook_tie_mode,
        minimum_profit=minimum_profit,
        minimum_roi=minimum_roi,
        require_kalshi=require_kalshi,
        maximum_vig=maximum_vig,
        max_low_probability=max_low_probability,
    )


@dataclass(frozen=True)
class RunConfig:
    sport: SportSpec
    kalshi_series: str
    regions: tuple[str, ...]
    bookmakers: tuple[str, ...]
    payout: Decimal
    minimum_profit: Decimal
    minimum_roi: Decimal
    start_tolerance: timedelta
    hours_ahead: Decimal
    include_live: bool
    max_quote_age: timedelta | None
    sportsbook_stake_increment: Decimal
    kalshi_balance_precision: Decimal
    sportsbook_tie_mode: str
    use_kalshi: bool
    require_kalshi: bool
    timeout: float
    retries: int
    workers: int
    maximum_vig: Decimal | None = Decimal("0.05")
    max_low_probability: Decimal | None = None
    markets: tuple[str, ...] = ("h2h",)
    wager_amount: Decimal | None = None


@dataclass
class RunReport:
    config: RunConfig
    odds_events: int
    kalshi_events: int
    matched_events: int
    candidates: list[Candidate]
    diagnostics: list[str]
    quota: Mapping[str, str]


def _parallel_fetch(
    items: Iterable[Any], workers: int, function: Any
) -> tuple[dict[Any, Any], dict[Any, Exception]]:
    values: dict[Any, Any] = {}
    errors: dict[Any, Exception] = {}
    unique = list(dict.fromkeys(items))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(function, item): item for item in unique}
        for future in as_completed(futures):
            item = futures[future]
            try:
                values[item] = future.result()
            except Exception as exc:  # isolated upstream failures are diagnostic
                errors[item] = exc
    return values, errors


def run_finder(
    config: RunConfig,
    api_key: str,
    *,
    odds_cache: OddsResponseCache | None = None,
) -> RunReport:
    now = datetime.now(timezone.utc)
    request_anchor = now.replace(
        minute=now.minute - now.minute % 5,
        second=0,
        microsecond=0,
    )
    http = JsonHttpClient(timeout=config.timeout, retries=config.retries)
    odds_client = OddsApiClient(http, api_key, cache=odds_cache)
    from_time = (
        request_anchor - timedelta(hours=12)
        if config.include_live
        else request_anchor
    )
    to_time = request_anchor + timedelta(hours=float(config.hours_ahead))
    main_markets = tuple(
        dict.fromkeys(
            ("h2h",)
            + tuple(
                market
                for market in config.markets
                if market in {"h2h", "spreads", "totals"}
            )
        )
    )
    sport_keys = resolve_odds_sport_keys(odds_client, config.sport)
    raw_odds: list[dict[str, Any]] = []
    quota: dict[str, str] = {}
    fetch_diagnostics: list[str] = []
    for sport_key in sport_keys:
        try:
            rows, current_quota = odds_client.get_odds(
                sport_key,
                regions=config.regions,
                bookmakers=config.bookmakers,
                markets=main_markets,
                commence_from=from_time,
                commence_to=to_time,
            )
        except FinderError as exc:
            fetch_diagnostics.append(f"Could not load {sport_key}: {exc}")
            continue
        raw_odds.extend(rows)
        quota.update(current_quota)
    if not raw_odds and fetch_diagnostics:
        raise FinderError("; ".join(fetch_diagnostics))
    odds_events, diagnostics = parse_odds_events(
        raw_odds,
        max_quote_age=config.max_quote_age,
        now=now,
    )
    diagnostics.extend(fetch_diagnostics)
    if "player_anytime_td" in config.markets:
        if config.sport.odds_key != "americanfootball_nfl":
            diagnostics.append("Anytime touchdown is currently available for NFL only")
        else:
            def fetch_touchdowns(item: tuple[str, str]) -> dict[str, Any]:
                sport_key, event_id = item
                data, _ = odds_client.get_event_odds(
                    sport_key,
                    event_id,
                    regions=config.regions,
                    bookmakers=config.bookmakers,
                    markets=("player_anytime_td",),
                )
                return data

            prop_payloads, prop_errors = _parallel_fetch(
                (
                    (
                        str(event.get("sport_key", config.sport.odds_key)),
                        str(event.get("id", "")),
                    )
                    for event in raw_odds
                ),
                config.workers,
                fetch_touchdowns,
            )
            odds_events.extend(
                parse_anytime_td_events(
                    list(prop_payloads.values()),
                    max_quote_age=config.max_quote_age,
                    now=now,
                )
            )
            for event_key, exc in prop_errors.items():
                diagnostics.append(
                    f"Could not fetch touchdown props for {event_key}: {exc}"
                )

    selected_events = [
        event for event in odds_events if event.market_key in config.markets
    ]

    def make_candidate(
        event: OddsEvent,
        routes: Sequence[KalshiRoute],
        match: MatchedEvent | None,
    ) -> Candidate | None:
        if match is None:
            placeholder = KalshiEvent(
                event_ticker="",
                series_ticker="",
                title="",
                occurrence_time=event.commence_time,
                mutually_exclusive=True,
                participants=(),
            )
            match = MatchedEvent(event, placeholder, {}, 0.0, timedelta())
        return build_candidate(
            match,
            routes,
            target_payout=config.wager_amount or config.payout,
            wager_amount=config.wager_amount,
            stake_increment=config.sportsbook_stake_increment,
            balance_precision=config.kalshi_balance_precision,
            tie_possible=(
                config.sport.tie_possible and event.market_key == "h2h"
            ),
            sportsbook_tie_mode=config.sportsbook_tie_mode,
            minimum_profit=config.minimum_profit,
            minimum_roi=config.minimum_roi,
            require_kalshi=config.require_kalshi,
            maximum_vig=config.maximum_vig,
            max_low_probability=config.max_low_probability,
        )

    if not config.use_kalshi:
        candidates = [
            candidate
            for event in selected_events
            if (candidate := make_candidate(event, (), None)) is not None
        ]
        candidates.sort(key=candidate_sort_key)
        return RunReport(
            config, len(selected_events), 0, 0, candidates, diagnostics, quota
        )

    kalshi_client = KalshiApiClient(http, cache=odds_cache)
    series = kalshi_client.get_series(config.kalshi_series)
    base_fee = series_fee_model(series)
    raw_kalshi = kalshi_client.get_open_events(config.kalshi_series)
    targets = kalshi_client.get_structured_targets(collect_target_ids(raw_kalshi))
    kalshi_events, kalshi_diagnostics = parse_kalshi_events(
        raw_kalshi,
        targets,
        series_title=str(series.get("title", "")),
    )
    diagnostics.extend(kalshi_diagnostics)
    base_odds_events = [event for event in odds_events if event.market_key == "h2h"]
    base_matches, match_diagnostics = match_events(
        base_odds_events,
        kalshi_events,
        tolerance=config.start_tolerance,
    )
    diagnostics.extend(match_diagnostics)
    base_by_id = {
        match.odds.base_event_id or match.odds.event_id.split(":", 1)[0]: match
        for match in base_matches
    }
    comparison_matches: dict[str, MatchedEvent] = {}
    route_specs: dict[str, list[KalshiRouteSpec]] = {}
    derivative_event_count = 0

    if "h2h" in config.markets:
        for match in base_matches:
            comparison_matches[match.odds.event_id] = match

    derivative_series = {
        "spreads": config.sport.kalshi_spread_series,
        "totals": config.sport.kalshi_total_series,
        "player_anytime_td": (
            "KXNFLTD"
            if config.sport.odds_key == "americanfootball_nfl"
            else None
        ),
    }
    for market_key in config.markets:
        series_ticker = derivative_series.get(market_key)
        comparisons = [
            event for event in selected_events if event.market_key == market_key
        ]
        if not comparisons or not series_ticker:
            continue
        try:
            derivative_series_data = kalshi_client.get_series(series_ticker)
            derivative_fee = series_fee_model(derivative_series_data)
            raw_derivatives = kalshi_client.get_open_events(series_ticker)
            derivative_targets = kalshi_client.get_structured_targets(
                collect_target_ids(raw_derivatives)
            )
        except FinderError as exc:
            diagnostics.append(f"Could not load Kalshi {market_key}: {exc}")
            continue
        derivative_event_count += len(raw_derivatives)
        matched_raw: dict[str, Mapping[str, Any] | None] = {}
        for comparison in comparisons:
            base_id = comparison.base_event_id or ""
            base_match = base_by_id.get(base_id)
            if base_match is None:
                continue
            if base_id not in matched_raw:
                matched_raw[base_id] = match_derivative_event(
                    base_match.kalshi,
                    raw_derivatives,
                    tolerance=config.start_tolerance,
                )
            raw_event = matched_raw[base_id]
            if raw_event is None:
                continue
            event_fee = raw_event_fee_model(derivative_fee, raw_event)
            specs = derivative_route_specs(
                comparison,
                base_match,
                raw_event,
                derivative_targets,
                event_fee,
            )
            if not specs:
                continue
            derivative_event = KalshiEvent(
                event_ticker=str(raw_event.get("event_ticker", "")),
                series_ticker=series_ticker,
                title=str(raw_event.get("title", "")),
                occurrence_time=_event_occurrence(raw_event)
                or base_match.kalshi.occurrence_time,
                mutually_exclusive=bool(raw_event.get("mutually_exclusive")),
                participants=base_match.kalshi.participants,
                series_title=str(derivative_series_data.get("title", "")),
            )
            comparison_matches[comparison.event_id] = MatchedEvent(
                comparison,
                derivative_event,
                base_match.participant_to_selection,
                base_match.team_score,
                abs(derivative_event.occurrence_time - comparison.commence_time),
            )
            route_specs[comparison.event_id] = specs

    tickers: list[str] = [
        str(participant.market.get("ticker"))
        for match in base_matches
        if "h2h" in config.markets
        for participant in match.kalshi.participants
        if participant.market.get("ticker")
    ]
    tickers.extend(
        spec.market_ticker for specs in route_specs.values() for spec in specs
    )
    raw_books, book_errors = _parallel_fetch(
        tickers, config.workers, kalshi_client.get_orderbook
    )
    books: dict[str, ParsedOrderbook] = {}
    for ticker, payload in raw_books.items():
        try:
            books[ticker] = parse_orderbook(payload)
        except FinderError as exc:
            diagnostics.append(f"Skipped Kalshi orderbook {ticker}: {exc}")
    for ticker, exc in book_errors.items():
        diagnostics.append(f"Could not fetch Kalshi orderbook {ticker}: {exc}")

    # The Events response carries the current event-level override. The
    # fee-changes endpoint is a schedule/feed and is not a reliable snapshot.
    event_fees: dict[str, FeeModel] = {
        match.kalshi.event_ticker: current_event_fee_model(base_fee, match.kalshi)
        for match in base_matches
    }

    candidates = []
    for event in selected_events:
        match = comparison_matches.get(event.event_id)
        routes: list[KalshiRoute] = []
        if match is not None and event.market_key == "h2h":
            fee_model = event_fees.get(match.kalshi.event_ticker)
            if fee_model is None:
                continue
            routes, route_diagnostics = make_kalshi_routes(
                match,
                books,
                fee_model,
                tie_possible=config.sport.tie_possible,
            )
            diagnostics.extend(route_diagnostics)
        elif match is not None:
            routes = [
                route
                for spec in route_specs.get(event.event_id, ())
                if (route := materialize_route(spec, books)) is not None
            ]
        candidate = make_candidate(event, routes, match)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=candidate_sort_key)
    return RunReport(
        config=config,
        odds_events=len(selected_events),
        kalshi_events=len(kalshi_events) + derivative_event_count,
        matched_events=len(comparison_matches),
        candidates=candidates,
        diagnostics=diagnostics,
        quota=quota,
    )


def money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01')):,.2f}"


def percent(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.01'))}%"


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def kalshi_event_url(event: KalshiEvent) -> str | None:
    """Build the public Kalshi page URL for a matched event."""

    if not event.series_ticker or not event.event_ticker:
        return None
    slug_source = event.series_title or event.title or event.series_ticker
    normalized = unicodedata.normalize("NFKD", slug_source)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")
    if not slug:
        slug = event.series_ticker.casefold()
    return (
        "https://kalshi.com/markets/"
        f"{event.series_ticker.casefold()}/{slug}/{event.event_ticker.casefold()}"
    )


def candidate_dict(candidate: Candidate) -> dict[str, Any]:
    event = candidate.match.odds
    return {
        "event": {
            "odds_event_id": event.event_id,
            "kalshi_event_ticker": candidate.match.kalshi.event_ticker or None,
            "kalshi_url": kalshi_event_url(candidate.match.kalshi),
            "sport": event.sport_key,
            "commence_time": iso_z(event.commence_time),
            "home_team": event.home_team,
            "away_team": event.away_team,
            "market_key": event.market_key,
            "market_label": event.market_label,
            "line": decimal_text(event.line) if event.line is not None else None,
            "team_match_score": round(candidate.match.team_score, 4),
            "start_time_difference_seconds": int(
                candidate.match.time_delta.total_seconds()
            ),
        },
        "eligible": candidate.eligible,
        "rejection_reason": candidate.rejection_reason,
        "uses_kalshi": candidate.uses_kalshi,
        "implied_probabilities": {
            name: decimal_text(value)
            for name, value in candidate.implied_probabilities.items()
        },
        "implied_probability_sum": decimal_text(
            candidate.implied_probability_sum
        ),
        "vig": decimal_text(candidate.vig),
        "vig_gap": decimal_text(candidate.vig_gap),
        "low_probability": decimal_text(candidate.low_probability),
        "probability_spread": decimal_text(candidate.probability_spread),
        "total_cost": decimal_text(candidate.total_cost),
        "win_profit": decimal_text(candidate.win_profit),
        "win_roi": decimal_text(candidate.win_roi),
        "worst_case_profit": decimal_text(candidate.worst_case_profit),
        "scenario_profits": {
            name: decimal_text(value)
            for name, value in candidate.scenario_profits.items()
        },
        "legs": [
            {
                "selection": leg.selection,
                "source_type": leg.source_type,
                "venue": leg.venue,
                "cost": decimal_text(leg.cost),
                "win_return": decimal_text(leg.win_return),
                "tie_return": decimal_text(leg.tie_return)
                if leg.tie_return is not None
                else None,
                **dict(leg.detail),
            }
            for leg in candidate.legs
        ],
    }


def report_dict(report: RunReport, *, include_rejected: int = 0) -> dict[str, Any]:
    eligible = [candidate for candidate in report.candidates if candidate.eligible]
    rejected = [candidate for candidate in report.candidates if not candidate.eligible]
    return {
        "generated_at": iso_z(datetime.now(timezone.utc)),
        "sport": report.config.sport.odds_key,
        "kalshi_series": report.config.kalshi_series
        if report.config.use_kalshi
        else None,
        "target_payout": (
            decimal_text(report.config.payout)
            if report.config.wager_amount is None
            else None
        ),
        "wager_amount": (
            decimal_text(report.config.wager_amount)
            if report.config.wager_amount is not None
            else None
        ),
        "maximum_vig": (
            decimal_text(report.config.maximum_vig)
            if report.config.maximum_vig is not None
            else None
        ),
        "max_low_probability": (
            decimal_text(report.config.max_low_probability)
            if report.config.max_low_probability is not None
            else None
        ),
        "counts": {
            "odds_events": report.odds_events,
            "kalshi_events": report.kalshi_events,
            "matched_events": report.matched_events,
            "opportunities": len(eligible),
        },
        "quota": dict(report.quota),
        "opportunities": [candidate_dict(item) for item in eligible],
        "near_misses": [candidate_dict(item) for item in rejected[:include_rejected]],
        "diagnostics": report.diagnostics,
    }


def _leg_summary(leg: EvaluatedQuote) -> str:
    if leg.source_type == "kalshi":
        return (
            f"Kalshi BUY {leg.detail['side']} {leg.detail['market_ticker']} | "
            f"{leg.detail['contracts']} contracts @ avg "
            f"${leg.detail['average_price']} + fee {money(decimal_value(leg.detail['fee']))}"
        )
    return (
        f"{leg.venue} ({leg.detail['bookmaker_key']}) | American "
        f"{leg.detail['american_odds']} | stake "
        f"{money(decimal_value(leg.detail['stake']))}"
        + (
            f" | line {leg.detail['point']}"
            if leg.detail.get("point") is not None
            else ""
        )
    )


def print_table(report: RunReport, *, show_near_misses: int, verbose: bool) -> None:
    eligible = [candidate for candidate in report.candidates if candidate.eligible]
    rejected = [candidate for candidate in report.candidates if not candidate.eligible]
    print(
        f"{report.config.sport.label}: {report.odds_events} sportsbook markets, "
        f"{report.kalshi_events} Kalshi events, {report.matched_events} matched"
    )
    if report.quota:
        remaining = report.quota.get("x-requests-remaining", "?")
        used = report.quota.get("x-requests-used", "?")
        print(f"The Odds API quota: {remaining} remaining, {used} used")
    print(
        f"Target gross payout per outcome: {money(report.config.payout)} | "
        f"close pairs: {len(eligible)}"
    )
    print()
    for number, candidate in enumerate(eligible, start=1):
        event = candidate.match.odds
        print(
            f"{number}. {event.away_team} at {event.home_team} - "
            f"{iso_z(event.commence_time)}"
        )
        print(f"   Market: {event.market_label}")
        for leg in candidate.legs:
            leg_probability = candidate.implied_probabilities[leg.selection]
            marker = (
                " <- lower-probability leg"
                if leg_probability == candidate.low_probability
                else ""
            )
            print(
                f"   {leg.selection}: {_leg_summary(leg)} | implied "
                f"{percent(leg_probability)}{marker}"
            )
            similar = leg.detail.get("similar_sportsbooks", [])
            if similar:
                alternatives = ", ".join(
                    f"{item['venue']} {item['american_odds']}"
                    for item in similar
                )
                print(f"      Similar books (within 1pp): {alternatives}")
        scenarios = ", ".join(
            f"{name} {money(profit)}"
            for name, profit in candidate.scenario_profits.items()
        )
        print(
            f"   Implied sum {percent(candidate.implied_probability_sum)} | "
            f"vig {percent(candidate.vig)} | gap from 100% "
            f"{percent(candidate.vig_gap)} | lower leg "
            f"{percent(candidate.low_probability)}"
        )
        print(f"   Scenario profit: {scenarios}")
        print()
    if not eligible:
        print("No pairs met the vig, low-probability, and modeled-risk filters.\n")

    if show_near_misses and rejected:
        print(f"Closest/rejected combinations (up to {show_near_misses}):")
        for candidate in rejected[:show_near_misses]:
            event = candidate.match.odds
            print(
                f"- {event.away_team} at {event.home_team}: "
                f"vig gap {percent(candidate.vig_gap)}, lower leg "
                f"{percent(candidate.low_probability)}; "
                f"{candidate.rejection_reason}"
            )
            for leg in candidate.legs:
                print(f"    {leg.selection}: {_leg_summary(leg)}")
        print()
    if verbose and report.diagnostics:
        print("Diagnostics:")
        for message in report.diagnostics:
            print(f"- {message}")
        print()
    print(
        "Market-data snapshot only: verify prices, limits, promo terms, and both "
        "venues' settlement/void rules before trading. Quotes can move between calls."
    )


def print_csv(report: RunReport, *, include_rejected: int) -> None:
    output = io.StringIO()
    fields = [
        "eligible",
        "reason",
        "commence_time",
        "market",
        "away_team",
        "home_team",
        "kalshi_event_ticker",
        "total_cost",
        "win_profit",
        "win_roi",
        "implied_probability_sum",
        "vig",
        "vig_gap",
        "low_probability",
        "worst_case_profit",
        "legs_json",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    eligible = [item for item in report.candidates if item.eligible]
    rejected = [item for item in report.candidates if not item.eligible]
    for candidate in eligible + rejected[:include_rejected]:
        event = candidate.match.odds
        writer.writerow(
            {
                "eligible": str(candidate.eligible).lower(),
                "reason": candidate.rejection_reason or "",
                "commence_time": iso_z(event.commence_time),
                "market": event.market_label,
                "away_team": event.away_team,
                "home_team": event.home_team,
                "kalshi_event_ticker": candidate.match.kalshi.event_ticker,
                "total_cost": candidate.total_cost,
                "win_profit": candidate.win_profit,
                "win_roi": candidate.win_roi,
                "implied_probability_sum": candidate.implied_probability_sum,
                "vig": candidate.vig,
                "vig_gap": candidate.vig_gap,
                "low_probability": candidate.low_probability,
                "worst_case_profit": candidate.worst_case_profit,
                "legs_json": json.dumps(candidate_dict(candidate)["legs"]),
            }
        )
    print(output.getvalue(), end="")


def comma_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def positive_decimal(value: str) -> Decimal:
    try:
        result = decimal_value(value)
    except FinderError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if result <= ZERO:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def nonnegative_decimal(value: str) -> Decimal:
    try:
        result = decimal_value(value)
    except FinderError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if result < ZERO:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find two-leg market pairs with a small implied-probability gap "
            "across sportsbooks and Kalshi. No orders are placed."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sport",
        choices=tuple(SPORTS),
        default="americanfootball_ncaaf",
        help="The Odds API sport key",
    )
    parser.add_argument(
        "--kalshi-series",
        help="Override the mapped Kalshi game series ticker",
    )
    parser.add_argument("--regions", default="us,us2", help="Comma-separated regions")
    parser.add_argument(
        "--bookmakers",
        default="",
        help=(
            "Comma-separated The Odds API bookmaker keys; blank requests all books "
            "in --regions"
        ),
    )
    parser.add_argument(
        "--markets",
        default="h2h",
        help=(
            "Comma-separated markets: h2h, spreads, totals, player_anytime_td "
            "(touchdowns are NFL only)"
        ),
    )
    parser.add_argument(
        "--payout",
        type=positive_decimal,
        default=Decimal("100"),
        help="Target gross payout in every win scenario",
    )
    parser.add_argument(
        "--min-profit",
        type=nonnegative_decimal,
        default=Decimal("0"),
        help="Optional strict-arbitrage profit floor; zero disables it",
    )
    parser.add_argument(
        "--min-roi",
        type=nonnegative_decimal,
        default=Decimal("0"),
        help="Optional strict-arbitrage ROI floor; zero disables it",
    )
    parser.add_argument(
        "--max-vig",
        type=nonnegative_decimal,
        default=Decimal("5"),
        help=(
            "Maximum absolute gap between the two implied probabilities' sum "
            "and 100, in percentage points"
        ),
    )
    parser.add_argument(
        "--max-low-probability",
        type=nonnegative_decimal,
        default=Decimal("100"),
        help=(
            "Require at least one leg at or below this implied probability "
            "percentage; 100 disables the filter"
        ),
    )
    parser.add_argument(
        "--hours-ahead",
        type=positive_decimal,
        default=Decimal("168"),
        help="Upcoming event horizon",
    )
    parser.add_argument(
        "--include-live", action="store_true", help="Also include recently started games"
    )
    parser.add_argument(
        "--start-tolerance-minutes",
        type=positive_decimal,
        default=Decimal("180"),
        help="Maximum kickoff difference when matching providers",
    )
    parser.add_argument(
        "--max-quote-age-minutes",
        type=nonnegative_decimal,
        default=Decimal("0"),
        help="Discard older sportsbook quotes; zero disables this filter",
    )
    parser.add_argument(
        "--sportsbook-tie",
        choices=("push", "loss"),
        default="push",
        help="Modeled treatment of a two-way sportsbook moneyline on an NFL tie",
    )
    parser.add_argument(
        "--sportsbook-stake-increment",
        type=positive_decimal,
        default=Decimal("0.01"),
        help="Sportsbook stake rounding increment in dollars",
    )
    parser.add_argument(
        "--kalshi-balance-precision",
        type=positive_decimal,
        default=Decimal("0.0001"),
        help="Conservative Kalshi cost rounding (direct accounts use $0.0001)",
    )
    parser.add_argument("--no-kalshi", action="store_true", help="Use sportsbooks only")
    parser.add_argument(
        "--require-kalshi",
        action="store_true",
        help="Only emit combinations whose selected legs include Kalshi",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retry count")
    parser.add_argument("--workers", type=int, default=8, help="Parallel Kalshi reads")
    parser.add_argument(
        "--format", choices=("table", "json", "csv"), default="table"
    )
    parser.add_argument(
        "--show-near-misses",
        type=int,
        default=0,
        help="Include this many rejected best-price combinations",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show unmatched/skipped diagnostics"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="Prompt for common parameters"
    )
    parser.add_argument(
        "--ui", action="store_true", help="Launch the local browser interface"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help=argparse.SUPPRESS
    )
    parser.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-browser", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not auto-prompt when launched with no arguments in a terminal",
    )
    parser.add_argument(
        "--list-sports", action="store_true", help="List built-in sport mappings and exit"
    )
    return parser


def _prompt(label: str, default: str) -> str:
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def interactive_config(args: argparse.Namespace) -> argparse.Namespace:
    print("Promo odds finder setup\n")
    entries = list(SPORTS.values())
    for index, spec in enumerate(entries, start=1):
        marker = " (default)" if spec.odds_key == args.sport else ""
        print(
            f"  {index}. {spec.label}: {spec.odds_key} -> "
            f"{spec.kalshi_series}{marker}"
        )
    default_index = next(
        index
        for index, spec in enumerate(entries, start=1)
        if spec.odds_key == args.sport
    )
    chosen = _prompt("Sport number or API sport key", str(default_index))
    if chosen.isdigit() and 1 <= int(chosen) <= len(entries):
        args.sport = entries[int(chosen) - 1].odds_key
    elif chosen in SPORTS:
        args.sport = chosen
    else:
        raise FinderError(f"Unsupported sport choice: {chosen}")
    args.regions = _prompt("Bookmaker regions", args.regions)
    args.bookmakers = _prompt(
        "Bookmaker keys (comma-separated; 'all' uses regions)",
        args.bookmakers or "all",
    )
    if args.bookmakers.casefold() == "all":
        args.bookmakers = ""
    args.markets = _prompt(
        "Markets (h2h,spreads,totals,player_anytime_td)", args.markets
    )
    args.payout = positive_decimal(_prompt("Target payout ($)", str(args.payout)))
    args.max_vig = nonnegative_decimal(
        _prompt("Maximum vig gap from 100% (percentage points)", str(args.max_vig))
    )
    args.max_low_probability = nonnegative_decimal(
        _prompt(
            "Maximum lower-leg implied probability % (100 disables)",
            str(args.max_low_probability),
        )
    )
    args.hours_ahead = positive_decimal(
        _prompt("Upcoming horizon (hours)", str(args.hours_ahead))
    )
    args.show_near_misses = int(
        _prompt("Near misses to display", str(args.show_near_misses))
    )
    print()
    return args


def config_from_args(args: argparse.Namespace) -> RunConfig:
    sport = SPORTS[args.sport]
    markets = comma_values(args.markets)
    allowed_markets = {"h2h", "spreads", "totals", "player_anytime_td"}
    unknown_markets = set(markets) - allowed_markets
    if not markets or unknown_markets:
        raise FinderError(
            "--markets must contain h2h, spreads, totals, and/or "
            "player_anytime_td"
        )
    if "player_anytime_td" in markets and sport.odds_key != "americanfootball_nfl":
        raise FinderError("player_anytime_td is available only for NFL")
    if sport.odds_key.startswith("tennis_") and set(markets) != {"h2h"}:
        raise FinderError("Tennis currently supports the moneyline market only")
    if args.no_kalshi and args.require_kalshi:
        raise FinderError("--no-kalshi and --require-kalshi cannot be combined")
    if args.timeout <= 0 or args.retries < 0 or args.workers <= 0:
        raise FinderError("timeout/workers must be positive and retries nonnegative")
    if args.max_low_probability > Decimal("100"):
        raise FinderError("--max-low-probability cannot exceed 100")
    max_age = (
        timedelta(minutes=float(args.max_quote_age_minutes))
        if args.max_quote_age_minutes > ZERO
        else None
    )
    return RunConfig(
        sport=sport,
        kalshi_series=args.kalshi_series or sport.kalshi_series,
        regions=comma_values(args.regions),
        bookmakers=comma_values(args.bookmakers),
        payout=args.payout,
        minimum_profit=args.min_profit,
        minimum_roi=args.min_roi / Decimal("100"),
        start_tolerance=timedelta(minutes=float(args.start_tolerance_minutes)),
        hours_ahead=args.hours_ahead,
        include_live=args.include_live,
        max_quote_age=max_age,
        sportsbook_stake_increment=args.sportsbook_stake_increment,
        kalshi_balance_precision=args.kalshi_balance_precision,
        sportsbook_tie_mode=args.sportsbook_tie,
        use_kalshi=not args.no_kalshi,
        require_kalshi=args.require_kalshi,
        timeout=args.timeout,
        retries=args.retries,
        workers=args.workers,
        maximum_vig=args.max_vig / Decimal("100"),
        max_low_probability=(
            args.max_low_probability / Decimal("100")
            if args.max_low_probability < Decimal("100")
            else None
        ),
        markets=markets,
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv_list)
    if args.ui:
        from arbitrage_finder_ui import launch_ui

        launch_ui(host=args.host, port=args.port, open_browser=not args.no_browser)
        return 0
    if args.list_sports:
        for spec in SPORTS.values():
            tie = " (tie scenario modeled)" if spec.tie_possible else ""
            print(f"{spec.odds_key:30} {spec.kalshi_series:18} {spec.label}{tie}")
        return 0
    should_prompt = args.interactive or (
        not argv_list
        and not args.non_interactive
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    try:
        if should_prompt:
            args = interactive_config(args)
        config = config_from_args(args)
        api_key = os.environ.get("THE_ODDS_API_KEY", "").strip()
        report = run_finder(config, api_key)
    except (FinderError, argparse.ArgumentTypeError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(
            json.dumps(
                report_dict(report, include_rejected=max(0, args.show_near_misses)),
                indent=2,
            )
        )
    elif args.format == "csv":
        print_csv(report, include_rejected=max(0, args.show_near_misses))
    else:
        print_table(
            report,
            show_near_misses=max(0, args.show_near_misses),
            verbose=args.verbose,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
