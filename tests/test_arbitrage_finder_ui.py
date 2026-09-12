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
                "payout": 100,
                "include_kalshi": True,
                "require_kalshi": True,
            }
        )
        self.assertEqual(config.maximum_vig, Decimal("0.025"))
        self.assertEqual(config.max_low_probability, Decimal("0.35"))
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
        self.assertIn("Find close odds", ui.PAGE)
        self.assertIn("American", ui.PAGE)
        self.assertIn("Similar books within 1pp", ui.PAGE)

    def test_bookmaker_control_is_a_multi_select_dropdown(self) -> None:
        self.assertIn('class="multi" id="bookmakerMenu"', ui.PAGE)
        self.assertIn(
            'class="bookmaker" type="checkbox" value="draftkings"', ui.PAGE
        )
        self.assertIn(
            'class="bookmaker" type="checkbox" value="fanduel"', ui.PAGE
        )
        self.assertIn("selectedBookmakers()", ui.PAGE)


if __name__ == "__main__":
    unittest.main()
