"""Focused regression tests for :mod:`arbitrage_finder`.

The tests intentionally use recorded-shape dictionaries and in-memory HTTP
responses.  They do not call either upstream API and require only the Python
standard library.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import arbitrage_finder as finder


D = Decimal
START = datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc)


def sportsbook_offer(selection: str, odds: str) -> finder.SportsbookOffer:
    return finder.SportsbookOffer(
        selection=selection,
        bookmaker_key="testbook",
        bookmaker_title="Test Book",
        decimal_odds=D(odds),
        updated_at=START,
    )


def matched_two_team_event(
    *,
    home: str = "Alpha Wolves",
    away: str = "Beta Bears",
    home_offers: tuple[finder.SportsbookOffer, ...] = (),
    away_offers: tuple[finder.SportsbookOffer, ...] = (),
    tie_settlement: Decimal | None = None,
) -> finder.MatchedEvent:
    odds = finder.OddsEvent(
        event_id="odds-event-1",
        sport_key="americanfootball_ncaaf",
        commence_time=START,
        home_team=home,
        away_team=away,
        offers={home: list(home_offers), away: list(away_offers)},
    )
    alpha = finder.KalshiParticipant(
        identity="alpha-id",
        label="Alpha",
        aliases=("Alpha", "Alpha Wolves"),
        market={
            "ticker": "KXGAME-ALPHA",
            "notional_value_dollars": "1.0000",
            "fractional_trading_enabled": False,
        },
        tie_yes_settlement=tie_settlement,
    )
    beta = finder.KalshiParticipant(
        identity="beta-id",
        label="Beta",
        aliases=("Beta", "Beta Bears"),
        market={
            "ticker": "KXGAME-BETA",
            "notional_value_dollars": "1.0000",
            "fractional_trading_enabled": False,
        },
        tie_yes_settlement=tie_settlement,
    )
    kalshi = finder.KalshiEvent(
        event_ticker="KXGAME",
        series_ticker="KXNCAAFGAME",
        title="Beta at Alpha Winner?",
        occurrence_time=START,
        mutually_exclusive=True,
        participants=(alpha, beta),
    )
    return finder.MatchedEvent(
        odds=odds,
        kalshi=kalshi,
        participant_to_selection={"alpha-id": home, "beta-id": away},
        team_score=1.0,
        time_delta=timedelta(0),
    )


def build_candidate(
    match: finder.MatchedEvent,
    routes: tuple[finder.KalshiRoute, ...],
    *,
    tie_possible: bool,
) -> finder.Candidate | None:
    return finder.build_candidate(
        match,
        routes,
        target_payout=D("100"),
        stake_increment=D("0.01"),
        balance_precision=D("0.0001"),
        tie_possible=tie_possible,
        sportsbook_tie_mode="push",
        minimum_profit=D("0.01"),
        minimum_roi=D("0"),
        require_kalshi=True,
    )


class OrderbookTests(unittest.TestCase):
    def test_fixed_point_complements_and_depth_fill(self) -> None:
        payload = {
            "orderbook_fp": {
                # Current Kalshi fields are fixed-point dollar/count strings.
                # The deliberately shuffled rows also ensure price, not array
                # position, determines the best level.
                "yes_dollars": [
                    ["0.4200", "13.00"],
                    ["0.2000", "5.00"],
                ],
                "no_dollars": [
                    ["0.5600", "2.50"],
                    ["0.3000", "10.00"],
                ],
            }
        }

        book = finder.parse_orderbook(payload)
        self.assertEqual(book.yes_bids[0], finder.PriceLevel(D("0.2000"), D("5.00")))
        self.assertEqual(book.yes_bids[-1], finder.PriceLevel(D("0.4200"), D("13.00")))

        yes_asks = finder.asks_for_side(book, "yes")
        no_asks = finder.asks_for_side(book, "no")
        yes_bids = finder.bids_for_side(book, "yes")
        self.assertEqual(yes_asks[0], finder.PriceLevel(D("0.4400"), D("2.50")))
        self.assertEqual(no_asks[0], finder.PriceLevel(D("0.5800"), D("13.00")))
        self.assertEqual(yes_bids[0], finder.PriceLevel(D("0.4200"), D("13.00")))

        fill = finder.fill_levels(yes_asks, D("4.00"))
        self.assertIsNotNone(fill)
        fills, cost = fill  # type: ignore[misc]
        self.assertEqual(
            fills,
            (
                finder.FilledLevel(D("0.4400"), D("2.50")),
                finder.FilledLevel(D("0.7000"), D("1.50")),
            ),
        )
        self.assertEqual(cost, D("2.150000"))
        self.assertIsNone(finder.fill_levels(yes_asks, D("13.00")))

    def test_malformed_or_empty_books_never_create_free_quotes(self) -> None:
        book = finder.parse_orderbook(
            {
                "orderbook_fp": {
                    "yes_dollars": [
                        ["not-a-price", "100.00"],
                        ["0.5000", "0.00"],
                        ["1.1000", "10.00"],
                    ],
                    "no_dollars": [["0.5000", "bad-quantity"]],
                }
            }
        )
        self.assertEqual(book.yes_bids, ())
        self.assertEqual(book.no_bids, ())
        self.assertEqual(finder.asks_for_side(book, "yes"), ())

        route = finder.KalshiRoute(
            selection="Alpha Wolves",
            market_ticker="EMPTY",
            side="yes",
            asks=finder.asks_for_side(book, "yes"),
            notional=D("1"),
            fractional=False,
            tie_settlement=None,
            fee_model=finder.FeeModel("quadratic", D("0")),
        )
        self.assertIsNone(
            finder.evaluate_kalshi_route(
                route,
                target_payout=D("100"),
                balance_precision=D("0.0001"),
            )
        )
        with self.assertRaises(finder.FinderError):
            finder.parse_orderbook({})


class FeeTests(unittest.TestCase):
    def test_quadratic_trade_fee_rounds_each_fill_to_six_decimals(self) -> None:
        # Current Kalshi fee-rounding docs distinguish the model trade fee
        # (ceil to $0.000001) from the later account-balance alignment. The
        # evaluated route applies the configured $0.0001/$0.01 balance grid.
        model = finder.FeeModel("quadratic", D("1"))
        fee = model.taker_fee(
            (finder.FilledLevel(price=D("0.3301"), quantity=D("0.03")),)
        )
        self.assertEqual(fee, D("0.000465"))

    def test_limit_order_fee_is_one_quarter_of_taker_rate(self) -> None:
        model = finder.FeeModel("quadratic", D("1"))
        fill = (finder.FilledLevel(price=D("0.40"), quantity=D("1")),)

        self.assertEqual(model.taker_fee(fill), D("0.016800"))
        self.assertEqual(model.limit_fee(fill), D("0.004200"))


class ParticipantMatchingTests(unittest.TestCase):
    def test_reversed_kalshi_participants_map_to_explicit_sportsbook_teams(self) -> None:
        raw = {
            "event_ticker": "KXNFLGAME-26SEP20ARISEA",
            "series_ticker": "KXNFLGAME",
            "title": "Arizona at Seattle Winner?",
            "mutually_exclusive": True,
            # Kalshi's participant order is deliberately the reverse of the
            # OddsEvent.outcomes home/away order below.
            "markets": [
                {
                    "ticker": "KXNFLGAME-26SEP20ARISEA-ARI",
                    "status": "active",
                    "market_type": "binary",
                    "yes_sub_title": "Arizona",
                    "occurrence_datetime": "2026-09-20T17:00:00Z",
                },
                {
                    "ticker": "KXNFLGAME-26SEP20ARISEA-SEA",
                    "status": "active",
                    "market_type": "binary",
                    "yes_sub_title": "Seattle",
                    "occurrence_datetime": "2026-09-20T17:00:00Z",
                },
            ],
        }
        kalshi_events, parse_diagnostics = finder.parse_kalshi_events([raw], {})
        self.assertEqual(parse_diagnostics, [])
        odds = finder.OddsEvent(
            event_id="odds-ari-sea",
            sport_key="americanfootball_nfl",
            commence_time=START,
            home_team="Seattle Seahawks",
            away_team="Arizona Cardinals",
            offers={"Seattle Seahawks": [], "Arizona Cardinals": []},
        )

        matches, diagnostics = finder.match_events(
            [odds], kalshi_events, tolerance=timedelta(minutes=5)
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(matches), 1)
        mapped_by_label = {
            participant.label: matches[0].participant_to_selection[participant.identity]
            for participant in matches[0].kalshi.participants
        }
        self.assertEqual(
            mapped_by_label,
            {
                "Arizona": "Arizona Cardinals",
                "Seattle": "Seattle Seahawks",
            },
        )

    def test_ambiguous_new_york_assignment_is_rejected(self) -> None:
        odds = finder.OddsEvent(
            event_id="odds-ny",
            sport_key="americanfootball_nfl",
            commence_time=START,
            home_team="New York Giants",
            away_team="New York Jets",
            offers={"New York Giants": [], "New York Jets": []},
        )
        participants = (
            finder.KalshiParticipant(
                identity="ny-one",
                label="New York",
                aliases=("New York",),
                market={"ticker": "KX-NY-ONE"},
                tie_yes_settlement=D("0.5"),
            ),
            finder.KalshiParticipant(
                identity="ny-two",
                label="New York",
                aliases=("New York",),
                market={"ticker": "KX-NY-TWO"},
                tie_yes_settlement=D("0.5"),
            ),
        )
        kalshi = finder.KalshiEvent(
            event_ticker="KX-NY",
            series_ticker="KXNFLGAME",
            title="New York at New York",
            occurrence_time=START,
            mutually_exclusive=True,
            participants=participants,
        )

        assignment = finder.assign_participants(participants, odds.outcomes)
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.margin, 0.0)  # type: ignore[union-attr]
        matches, diagnostics = finder.match_events(
            [odds], [kalshi], tolerance=timedelta(minutes=5)
        )
        self.assertEqual(matches, [])
        self.assertTrue(any("Unmatched Odds event" in item for item in diagnostics))


class AdditionalMarketTests(unittest.TestCase):
    def test_american_odds_conversion_and_similar_book_grouping(self) -> None:
        self.assertEqual(finder.american_odds(D("2.50")), "+150")
        self.assertEqual(finder.american_odds(D("1.50")), "-200")
        offers = (
            sportsbook_offer("Alpha Wolves", "2.00"),
            finder.SportsbookOffer(
                "Alpha Wolves", "nearbook", "Near Book", D("1.98"), START
            ),
            finder.SportsbookOffer(
                "Alpha Wolves", "farbook", "Far Book", D("1.80"), START
            ),
        )
        quotes = [
            finder.evaluate_sportsbook_offer(
                offer,
                target_payout=D("100"),
                stake_increment=D("0.01"),
                tie_mode="push",
            )
            for offer in offers
        ]
        enriched = finder.attach_similar_sportsbooks(quotes)
        nearby = enriched[0].detail["similar_sportsbooks"]
        self.assertEqual([item["bookmaker_key"] for item in nearby], ["nearbook"])
        self.assertEqual(nearby[0]["american_odds"], "-102")

    def test_odds_parser_groups_spreads_and_totals_by_exact_line(self) -> None:
        payload = [
            {
                "id": "event-lines",
                "sport_key": "americanfootball_nfl",
                "commence_time": START.isoformat(),
                "home_team": "Alpha Wolves",
                "away_team": "Beta Bears",
                "bookmakers": [
                    {
                        "key": "testbook",
                        "title": "Test Book",
                        "last_update": START.isoformat(),
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Alpha Wolves", "price": 1.5},
                                    {"name": "Beta Bears", "price": 2.8},
                                ],
                            },
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": "Alpha Wolves", "price": 1.91, "point": -3.5},
                                    {"name": "Beta Bears", "price": 1.91, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": 1.95, "point": 47.5},
                                    {"name": "Under", "price": 1.87, "point": 47.5},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
        events, diagnostics = finder.parse_odds_events(
            payload, max_quote_age=None, now=START
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual({event.market_key for event in events}, {"h2h", "spreads", "totals"})
        spread = next(event for event in events if event.market_key == "spreads")
        total = next(event for event in events if event.market_key == "totals")
        self.assertEqual(spread.line, D("-3.5"))
        self.assertEqual(total.outcomes, ("Over 47.5", "Under 47.5"))

    def test_total_and_spread_routes_use_exact_kalshi_strikes(self) -> None:
        base = matched_two_team_event()
        fee = finder.FeeModel("quadratic", D("0"))
        total = finder.OddsEvent(
            "event:totals:47.5", "americanfootball_nfl", START,
            "Alpha Wolves", "Beta Bears",
            {"Over 47.5": [], "Under 47.5": []},
            market_key="totals", line=D("47.5"),
            selections=("Over 47.5", "Under 47.5"), base_event_id="event",
        )
        total_specs = finder.derivative_route_specs(
            total,
            base,
            {"markets": [{"ticker": "TOTAL-48", "status": "active", "floor_strike": 47.5}]},
            {},
            fee,
        )
        self.assertEqual([(item.side, item.selection) for item in total_specs], [
            ("yes", "Over 47.5"), ("no", "Under 47.5")
        ])

        spread = finder.OddsEvent(
            "event:spreads:-3.5", "americanfootball_nfl", START,
            "Alpha Wolves", "Beta Bears", {"Alpha Wolves": [], "Beta Bears": []},
            market_key="spreads", line=D("-3.5"), base_event_id="event",
        )
        spread_specs = finder.derivative_route_specs(
            spread,
            base,
            {"markets": [{
                "ticker": "SPREAD-A-4", "status": "active", "floor_strike": 3.5,
                "custom_strike": {"football_team": "alpha-id"},
            }]},
            {},
            fee,
        )
        self.assertEqual([(item.side, item.selection) for item in spread_specs], [
            ("yes", "Alpha Wolves"), ("no", "Beta Bears")
        ])


class CandidateTests(unittest.TestCase):
    def test_candidate_uses_only_direct_team_yes_markets(self) -> None:
        match = matched_two_team_event(
            away_offers=(sportsbook_offer("Beta Bears", "2.00"),)
        )
        fee_model = finder.FeeModel("quadratic", D("0"))

        books = {
            "KXGAME-ALPHA": finder.ParsedOrderbook(
                yes_bids=(finder.PriceLevel(D("0.20"), D("200")),),
                no_bids=(finder.PriceLevel(D("0.70"), D("200")),),
            ),
            "KXGAME-BETA": finder.ParsedOrderbook(
                yes_bids=(finder.PriceLevel(D("0.65"), D("200")),),
                no_bids=(finder.PriceLevel(D("0.20"), D("200")),),
            ),
        }
        routes, diagnostics = finder.make_kalshi_routes(
            match, books, fee_model, tie_possible=False
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual({route.side for route in routes}, {"yes"})
        self.assertEqual(
            {(route.selection, route.market_ticker) for route in routes},
            {
                ("Alpha Wolves", "KXGAME-ALPHA"),
                ("Beta Bears", "KXGAME-BETA"),
            },
        )
        candidate = build_candidate(match, tuple(routes), tie_possible=False)
        self.assertIsNotNone(candidate)
        alpha_leg = next(
            leg for leg in candidate.legs if leg.selection == "Alpha Wolves"  # type: ignore[union-attr]
        )
        self.assertEqual(alpha_leg.detail["market_ticker"], "KXGAME-ALPHA")
        self.assertEqual(alpha_leg.detail["side"], "YES")

    def test_promo_metrics_and_max_vig_filter(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.10"),),
            away_offers=(sportsbook_offer("Beta Bears", "1.95"),),
        )
        candidate = finder.build_candidate(
            match,
            (),
            target_payout=D("100"),
            stake_increment=D("0.01"),
            balance_precision=D("0.0001"),
            tie_possible=False,
            sportsbook_tie_mode="push",
            minimum_profit=D("0"),
            minimum_roi=D("0"),
            require_kalshi=False,
            maximum_vig=D("0.02"),
            max_low_probability=D("0.48"),
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.eligible)  # type: ignore[union-attr]
        self.assertLess(candidate.vig_gap, D("0.02"))  # type: ignore[union-attr]
        self.assertLess(candidate.low_probability, D("0.48"))  # type: ignore[union-attr]

    def test_wager_amount_sizes_kalshi_to_sportsbook_gross_return(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "10.00"),),
        )
        route = finder.KalshiRoute(
            selection="Beta Bears",
            market_ticker="KXGAME-BETA",
            side="yes",
            asks=(finder.PriceLevel(D("0.90"), D("2000")),),
            notional=D("1"),
            fractional=False,
            tie_settlement=None,
            fee_model=finder.FeeModel("quadratic", D("0")),
        )
        candidate = finder.build_candidate(
            match,
            (route,),
            target_payout=D("100"),
            wager_amount=D("100"),
            stake_increment=D("0.01"),
            balance_precision=D("0.0001"),
            tie_possible=False,
            sportsbook_tie_mode="push",
            minimum_profit=D("0"),
            minimum_roi=D("0"),
            require_kalshi=True,
        )
        self.assertIsNotNone(candidate)
        sportsbook_leg = next(
            leg for leg in candidate.legs if leg.source_type == "sportsbook"  # type: ignore[union-attr]
        )
        kalshi_leg = next(
            leg for leg in candidate.legs if leg.source_type == "kalshi"  # type: ignore[union-attr]
        )
        self.assertEqual(sportsbook_leg.detail["stake"], "100")
        self.assertEqual(sportsbook_leg.win_return, D("1000"))
        self.assertEqual(kalshi_leg.detail["contracts"], "1000")
        self.assertEqual(kalshi_leg.win_return, D("1000"))

    def test_require_kalshi_chooses_close_mixed_pair_not_sportsbook_pair(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.00"),),
            away_offers=(sportsbook_offer("Beta Bears", "2.00"),),
        )
        route = finder.KalshiRoute(
            selection="Beta Bears",
            market_ticker="KXGAME-BETA",
            side="yes",
            asks=(finder.PriceLevel(D("0.51"), D("200")),),
            notional=D("1"),
            fractional=False,
            tie_settlement=None,
            fee_model=finder.FeeModel("quadratic", D("0")),
        )
        candidate = finder.build_candidate(
            match,
            (route,),
            target_payout=D("100"),
            stake_increment=D("0.01"),
            balance_precision=D("0.0001"),
            tie_possible=False,
            sportsbook_tie_mode="push",
            minimum_profit=D("0"),
            minimum_roi=D("0"),
            require_kalshi=True,
            maximum_vig=D("0.02"),
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.eligible)  # type: ignore[union-attr]
        self.assertTrue(candidate.uses_kalshi)  # type: ignore[union-attr]
        self.assertEqual(candidate.vig_gap, D("0.01"))  # type: ignore[union-attr]

    def test_max_vig_filters_on_absolute_distance_from_one(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.20"),),
            away_offers=(sportsbook_offer("Beta Bears", "2.20"),),
        )
        candidate = finder.build_candidate(
            match,
            (),
            target_payout=D("100"),
            stake_increment=D("0.01"),
            balance_precision=D("0.0001"),
            tie_possible=False,
            sportsbook_tie_mode="push",
            minimum_profit=D("0"),
            minimum_roi=D("0"),
            require_kalshi=False,
            maximum_vig=D("0.01"),
        )
        self.assertIsNotNone(candidate)
        self.assertLess(candidate.vig, D("-0.09"))  # type: ignore[union-attr]
        self.assertFalse(candidate.eligible)  # type: ignore[union-attr]
        self.assertEqual(  # type: ignore[union-attr]
            candidate.rejection_reason, "vig gap is above threshold"
        )

    def test_nfl_tie_rejects_otherwise_profitable_mixed_source_pair(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.50"),),
            tie_settlement=D("0.5"),
        )
        beta_yes = finder.KalshiRoute(
            selection="Beta Bears",
            market_ticker="KXGAME-BETA",
            side="yes",
            asks=(finder.PriceLevel(D("0.55"), D("200")),),
            notional=D("1"),
            fractional=False,
            tie_settlement=D("0.5"),
            fee_model=finder.FeeModel("quadratic", D("0")),
        )

        candidate = build_candidate(match, (beta_yes,), tie_possible=True)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.win_profit, D("5.0000"))  # type: ignore[union-attr]
        self.assertEqual(candidate.scenario_profits["Tie"], D("-5.0000"))  # type: ignore[union-attr]
        self.assertFalse(candidate.eligible)  # type: ignore[union-attr]
        self.assertEqual(  # type: ignore[union-attr]
            candidate.rejection_reason, "negative modeled tie payoff"
        )

    def test_ncaaf_no_tie_accepts_real_mixed_source_arbitrage(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.50"),)
        )
        beta_yes = finder.KalshiRoute(
            selection="Beta Bears",
            market_ticker="KXGAME-BETA",
            side="yes",
            asks=(finder.PriceLevel(D("0.55"), D("200")),),
            notional=D("1"),
            fractional=False,
            tie_settlement=None,
            fee_model=finder.FeeModel("quadratic", D("0")),
        )

        candidate = build_candidate(match, (beta_yes,), tie_possible=False)
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.eligible)  # type: ignore[union-attr]
        self.assertEqual(candidate.total_cost, D("95.0000"))  # type: ignore[union-attr]
        self.assertEqual(candidate.worst_case_profit, D("5.0000"))  # type: ignore[union-attr]


class FakeHttpClient:
    def __init__(self, responses: list[finder.JsonResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(
        self,
        base_url: str,
        path: str = "",
        *,
        params: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> finder.JsonResponse:
        self.calls.append((base_url, path, dict(params or {})))
        if not self.responses:
            raise AssertionError("Fake HTTP client received an unexpected request")
        return self.responses.pop(0)


class ClientAndSerializationTests(unittest.TestCase):
    def test_odds_snapshot_cache_reuses_identical_request(self) -> None:
        http = FakeHttpClient(
            [finder.JsonResponse([], {"x-requests-remaining": "99"})]
        )
        cache = finder.OddsResponseCache(ttl_seconds=300)
        client = finder.OddsApiClient(  # type: ignore[arg-type]
            http, "secret", cache=cache
        )
        request = {
            "regions": ("us",),
            "bookmakers": (),
            "markets": ("h2h",),
            "commence_from": START,
            "commence_to": START + timedelta(hours=24),
        }

        _, first_quota = client.get_odds("americanfootball_ncaaf", **request)
        shifted_request = {
            **request,
            "commence_from": START + timedelta(minutes=1),
            "commence_to": START + timedelta(hours=24, minutes=1),
        }
        _, second_quota = client.get_odds(
            "americanfootball_ncaaf", **shifted_request
        )

        self.assertEqual(len(http.calls), 1)
        self.assertNotIn("x-snapshot-cache", first_quota)
        self.assertEqual(second_quota["x-snapshot-cache"], "reused")

    def test_market_snapshot_cache_reuses_kalshi_request(self) -> None:
        http = FakeHttpClient(
            [finder.JsonResponse({"orderbook_fp": {}}, {})]
        )
        cache = finder.OddsResponseCache(ttl_seconds=300)
        client = finder.KalshiApiClient(  # type: ignore[arg-type]
            http, cache=cache
        )

        first = client.get_orderbook("KX-MARKET")
        second = client.get_orderbook("KX-MARKET")

        self.assertEqual(first, second)
        self.assertEqual(len(http.calls), 1)

    def test_dynamic_tennis_and_preseason_sport_resolution(self) -> None:
        class MetadataClient:
            @staticmethod
            def get_sports() -> list[dict[str, object]]:
                return [
                    {"key": "tennis_atp_us_open", "active": True},
                    {"key": "tennis_atp_wimbledon", "active": False},
                    {"key": "americanfootball_nfl", "active": True},
                    {"key": "americanfootball_nfl_preseason", "active": True},
                ]

        client = MetadataClient()
        self.assertEqual(
            finder.resolve_odds_sport_keys(client, finder.SPORTS["tennis_atp"]),  # type: ignore[arg-type]
            ("tennis_atp_us_open",),
        )
        self.assertEqual(
            finder.resolve_odds_sport_keys(
                client, finder.SPORTS["americanfootball_nfl"]  # type: ignore[arg-type]
            ),
            ("americanfootball_nfl", "americanfootball_nfl_preseason"),
        )
        self.assertNotIn("icehockey_nhl_preseason", finder.SPORTS)

    def test_repeated_pagination_cursor_is_rejected(self) -> None:
        http = FakeHttpClient(
            [
                finder.JsonResponse(
                    {"events": [{"event_ticker": "EVENT-1"}], "cursor": "again"},
                    {},
                ),
                finder.JsonResponse(
                    {"events": [{"event_ticker": "EVENT-2"}], "cursor": "again"},
                    {},
                ),
            ]
        )
        client = finder.KalshiApiClient(http)  # type: ignore[arg-type]

        with self.assertRaisesRegex(finder.ApiError, "repeated a cursor"):
            client.get_open_events("KXNCAAFGAME")
        self.assertEqual(len(http.calls), 2)
        self.assertNotIn("cursor", http.calls[0][2])
        self.assertEqual(http.calls[1][2]["cursor"], "again")

    def test_report_is_json_serializable_and_preserves_kalshi_side(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.50"),)
        )
        route = finder.KalshiRoute(
            selection="Beta Bears",
            market_ticker="KXGAME-BETA",
            side="yes",
            asks=(finder.PriceLevel(D("0.55"), D("200")),),
            notional=D("1"),
            fractional=False,
            tie_settlement=None,
            fee_model=finder.FeeModel("quadratic", D("0")),
            bids=(finder.PriceLevel(D("0.54"), D("73")),),
        )
        candidate = build_candidate(match, (route,), tie_possible=False)
        self.assertIsNotNone(candidate)
        config = finder.RunConfig(
            sport=finder.SPORTS["americanfootball_ncaaf"],
            kalshi_series="KXNCAAFGAME",
            regions=("us",),
            bookmakers=(),
            payout=D("100"),
            minimum_profit=D("0.01"),
            minimum_roi=D("0"),
            start_tolerance=timedelta(minutes=180),
            hours_ahead=D("168"),
            include_live=False,
            max_quote_age=None,
            sportsbook_stake_increment=D("0.01"),
            kalshi_balance_precision=D("0.0001"),
            sportsbook_tie_mode="push",
            use_kalshi=True,
            require_kalshi=True,
            timeout=15.0,
            retries=0,
            workers=1,
        )
        report = finder.RunReport(
            config=config,
            odds_events=1,
            kalshi_events=1,
            matched_events=1,
            candidates=[candidate],  # type: ignore[list-item]
            diagnostics=[],
            quota={"x-requests-remaining": "99"},
        )

        encoded = json.dumps(finder.report_dict(report))
        decoded = json.loads(encoded)
        self.assertEqual(decoded["counts"]["opportunities"], 1)
        kalshi_leg = next(
            leg
            for leg in decoded["opportunities"][0]["legs"]
            if leg["source_type"] == "kalshi"
        )
        self.assertEqual(kalshi_leg["market_ticker"], "KXGAME-BETA")
        self.assertEqual(kalshi_leg["side"], "YES")
        self.assertEqual(kalshi_leg["current_price"], "0.55")
        self.assertEqual(kalshi_leg["current_price_volume"], "200")
        self.assertEqual(
            kalshi_leg["current_price_implied_probability"], "0.55"
        )
        self.assertEqual(kalshi_leg["one_cent_lower_bid_price"], "0.54")
        self.assertEqual(kalshi_leg["one_cent_lower_bid_volume"], "73")
        self.assertEqual(
            kalshi_leg["one_cent_lower_bid_implied_probability"], "0.54"
        )
        self.assertEqual(
            kalshi_leg["orderbook_levels"],
            [{"price": "0.55", "quantity": "200"}],
        )
        self.assertEqual(kalshi_leg["notional"], "1")
        self.assertEqual(kalshi_leg["maker_fee_rate"], "0.0175")
        sportsbook_leg = next(
            leg
            for leg in decoded["opportunities"][0]["legs"]
            if leg["source_type"] == "sportsbook"
        )
        self.assertEqual(sportsbook_leg["stake_increment"], "0.01")
        self.assertEqual(
            decoded["opportunities"][0]["event"]["kalshi_url"],
            "https://kalshi.com/markets/kxncaafgame/"
            "beta-at-alpha-winner/kxgame",
        )

    def test_candidates_sort_by_signed_vig(self) -> None:
        match = matched_two_team_event(
            home_offers=(sportsbook_offer("Alpha Wolves", "2.00"),),
            away_offers=(sportsbook_offer("Beta Bears", "2.00"),),
        )
        candidate = build_candidate(match, (), tie_possible=False)
        self.assertIsNotNone(candidate)
        positive = replace(candidate, vig=D("0.01"))
        negative = replace(candidate, vig=D("-0.02"))
        zero = replace(candidate, vig=D("0"))

        ordered = sorted(
            (positive, negative, zero), key=finder.candidate_sort_key
        )
        self.assertEqual(
            [item.vig for item in ordered],
            [D("-0.02"), D("0"), D("0.01")],
        )


if __name__ == "__main__":
    unittest.main()
