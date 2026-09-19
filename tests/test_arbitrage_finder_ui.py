"""Tests for the local browser UI's input translation."""

from decimal import Decimal
import unittest

import arbitrage_finder_ui as ui


class UiConfigTests(unittest.TestCase):
    def test_ui_percent_filters_are_converted_to_probabilities(self) -> None:
        config = ui._config(
            {
                "sport": "americanfootball_ncaaf",
                "max_vig": 2.5,
                "max_low_probability": 35,
                "hours_ahead": 48,
                "promotion_amount": 125,
                "include_kalshi": True,
                "require_kalshi": True,
            }
        )
        self.assertEqual(config.maximum_vig, Decimal("0.025"))
        self.assertEqual(config.max_low_probability, Decimal("0.35"))
        self.assertEqual(config.wager_amount, Decimal("125"))
        self.assertTrue(config.require_kalshi)

    def test_ui_accepts_multiple_market_types(self) -> None:
        config = ui._config(
            {
                "sport": "americanfootball_nfl",
                "markets": ["h2h", "spreads", "totals", "player_anytime_td"],
            }
        )
        self.assertEqual(
            config.markets,
            ("h2h", "spreads", "totals", "player_anytime_td"),
        )

    def test_tennis_is_moneyline_only(self) -> None:
        config = ui._config({"sport": "tennis_atp", "markets": ["h2h"]})
        self.assertEqual(config.sport.label, "ATP Tennis")
        with self.assertRaisesRegex(Exception, "moneyline"):
            ui._config({"sport": "tennis_wta", "markets": ["h2h", "totals"]})

    def test_ui_page_does_not_embed_an_api_key_field(self) -> None:
        self.assertNotIn("THE_ODDS_API_KEY", ui.PAGE)
        self.assertIn("Load market snapshot", ui.PAGE)
        self.assertIn("Apply filters", ui.PAGE)
        self.assertIn("American", ui.PAGE)
        self.assertIn("Similar books within 1pp", ui.PAGE)
        self.assertIn("Kalshi order book", ui.PAGE)
        self.assertIn("Taker ask", ui.PAGE)
        self.assertIn("Maker limit (1¢ lower)", ui.PAGE)
        self.assertIn("implied with taker fee", ui.PAGE)
        self.assertIn("implied with maker fee", ui.PAGE)
        self.assertIn("Open on Kalshi", ui.PAGE)
        self.assertIn('target="_blank" rel="noopener noreferrer"', ui.PAGE)

    def test_bookmaker_control_is_a_multi_select_dropdown(self) -> None:
        self.assertIn('class="multi" id="bookmakerMenu"', ui.PAGE)
        self.assertIn(
            'class="bookmaker" type="checkbox" value="draftkings"', ui.PAGE
        )
        self.assertIn(
            'class="bookmaker" type="checkbox" value="fanduel"', ui.PAGE
        )
        self.assertIn("selectedBookmakers()", ui.PAGE)
        self.assertIn('id="bookmakerDone"', ui.PAGE)
        self.assertIn("!bookmakerMenu.contains(event.target)", ui.PAGE)
        self.assertIn("event.key==='Escape'", ui.PAGE)
        self.assertNotIn("Select any number of books", ui.PAGE)

    def test_scope_filters_come_before_disabled_analysis_filters(self) -> None:
        self.assertIn('id="scopePanel"', ui.PAGE)
        self.assertIn('id="analysisFilters" disabled', ui.PAGE)
        self.assertIn('id="continue"', ui.PAGE)
        self.assertLess(ui.PAGE.index('id="sport"'), ui.PAGE.index('id="maxVig"'))
        self.assertLess(ui.PAGE.index('id="regions"'), ui.PAGE.index('id="maxVig"'))
        self.assertLess(
            ui.PAGE.index('id="bookmakerMenu"'), ui.PAGE.index('id="maxVig"')
        )
        self.assertLess(
            ui.PAGE.index('class="scope-market"'), ui.PAGE.index('id="maxVig"')
        )
        self.assertLess(ui.PAGE.index('id="hours"'), ui.PAGE.index('id="maxVig"'))
        self.assertLess(ui.PAGE.index('id="live"'), ui.PAGE.index('id="maxVig"'))
        self.assertLess(ui.PAGE.index('id="kalshi"'), ui.PAGE.index('id="maxVig"'))
        self.assertNotIn('id="amountWagered"', ui.PAGE)
        self.assertIn('class="filter-market"', ui.PAGE)
        self.assertIn('id="filterKalshi"', ui.PAGE)
        self.assertIn("refresh_prices:refreshPrices||loadScope", ui.PAGE)
        self.assertIn('id="refresh"', ui.PAGE)

    def test_promotion_inputs_and_client_side_sorting_are_present(self) -> None:
        self.assertIn('id="profitBoost"', ui.PAGE)
        self.assertIn('id="promoAmount"', ui.PAGE)
        self.assertIn('id="sortBy"', ui.PAGE)
        self.assertLess(ui.PAGE.index('id="status"'), ui.PAGE.index('id="sortBy"'))
        self.assertIn('class="sort-control"', ui.PAGE)
        self.assertIn('Vig (lowest to highest)', ui.PAGE)
        self.assertIn('Profit boost total made (highest first)', ui.PAGE)
        self.assertIn('Bonus bet total made (highest first)', ui.PAGE)
        self.assertIn("'profit_boost'", ui.PAGE)
        self.assertIn("'bonus_bet'", ui.PAGE)
        self.assertIn("function promotionMetrics(c)", ui.PAGE)
        self.assertIn("function hedgeQuote(leg,targetPayout,execution='taker')", ui.PAGE)
        self.assertIn("promo stake not returned", ui.PAGE)
        self.assertIn("contracts · cost", ui.PAGE)
        self.assertIn("wager ${money(leg.stake)}", ui.PAGE)
        self.assertIn("Taker price:", ui.PAGE)
        self.assertIn("Maker price:", ui.PAGE)
        self.assertIn("used for filtering and sorting", ui.PAGE)
        self.assertIn("comparison only", ui.PAGE)
        self.assertIn("maker fee", ui.PAGE)
        self.assertIn("Total made:", ui.PAGE)
        self.assertIn("totalMade??-Infinity", ui.PAGE)
        self.assertIn(
            "Assumes the resting limit order fills completely", ui.PAGE
        )


if __name__ == "__main__":
    unittest.main()
