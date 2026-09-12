# Sports betting tools

## Interactive promo odds finder

[`arbitrage_finder.py`](arbitrage_finder.py) compares opposing head-to-head
prices from The Odds API and Kalshi. Its primary job is to surface two-leg pairs
with the smallest vig, especially pairs with a low-probability leg that may be
useful for a promotional bet. It only reads market data and never places orders.

For each pair, the finder converts both executable prices to implied
probabilities and calculates:

- **implied sum** = leg A probability + leg B probability;
- **vig** = implied sum - 100% (negative means an arbitrage price);
- **vig gap** = absolute distance between the implied sum and 100%; and
- **lower leg** = the smaller of the two implied probabilities.

Results are ranked by absolute vig gap first, then lower-leg probability. The
maximum vig filter applies to that absolute distance from 100%, whether the vig
is positive or negative. Use the filters to decide what counts as close;
automatic promo-bet sizing is intentionally left for a later feature.

Sportsbook prices are displayed as American odds. Under each selected leg, the
UI also lists other sportsbooks whose price is within one implied-probability
percentage point of the selected price. The implied-probability comparison
keeps the tolerance consistent for favorites and longshots.

The script uses only the Python standard library (Python 3.10+). Put your Odds
API key in `.env`:

```dotenv
THE_ODDS_API_KEY=your_key_here
```

Kalshi's public events, structured-target, fee, and single-market order-book
endpoints do not require a Kalshi API key for this read-only workflow.

Launch the browser UI:

```powershell
python arbitrage_finder.py --ui
```

This serves a private local page at `http://127.0.0.1:8765/`. The API key stays
in Python and is never sent to browser code. Stop it with `Ctrl+C` in the
terminal. The Bookmakers dropdown supports selecting any number of sportsbooks;
leave **All bookmakers** selected to use every book returned for the chosen
regions.

The Markets controls support:

- **Moneyline** for every configured sport;
- **Spreads** and **Totals** for NFL, NCAAF, NBA, WNBA, NCAAB, MLB, and NHL;
- **NFL anytime TD**, pairing a sportsbook's player-to-score YES price with
  BUY NO on the exact matched Kalshi `1+ touchdowns` player contract.
- **ATP Tennis** and **WTA Tennis** moneylines. Active tournaments are resolved
  from The Odds API at runtime and matched to Kalshi's `KXATPMATCH` and
  `KXWTAMATCH` series.

Spreads and totals are compared only at identical lines. For example, `-3.5`
is paired with `+3.5`, and `Over 47.5` with `Under 47.5`; a nearby line is never
silently substituted. NFL touchdown props require a separate Odds API request
per game, so selecting that market consumes more API quota than the game-level
markets. Player-prop void and eligibility rules can differ by sportsbook, so
verify both venues' rules before using a touchdown pair.

Selecting a main NFL, NBA, MLB, or NHL category automatically adds that
league's active `*_preseason` feed. Preseason therefore appears inside the main
league results instead of as a separate sport choice. Tennis retirement,
walkover, and minimum-play rules vary between books; verify settlement terms
before treating a tennis pair as a hedge.

Launch the terminal-guided setup instead:

```powershell
python arbitrage_finder.py --interactive
```

Or make a reproducible one-shot query:

```powershell
python arbitrage_finder.py `
  --sport americanfootball_ncaaf `
  --markets h2h,spreads,totals `
  --bookmakers draftkings,fanduel,betmgm,williamhill_us `
  --payout 100 `
  --max-vig 3 `
  --max-low-probability 35 `
  --show-near-misses 5
```

`--max-vig 3` keeps pairs whose implied sum is within 3 percentage points of
100%. `--max-low-probability 35` requires at least one leg at 35% implied
probability or lower. Use `100` to disable the low-leg filter. `--payout` is a
pricing amount used to account for Kalshi depth and fees; it does not place or
size a bet.

Useful commands:

```powershell
python arbitrage_finder.py --help
python arbitrage_finder.py --list-sports
python arbitrage_finder.py --format json --non-interactive
python arbitrage_finder.py --format csv --show-near-misses 10
python -m unittest discover -s tests -v
```

Built-in league mappings:

| The Odds API sport | Kalshi game | Spread | Total |
| --- | --- | --- | --- |
| `americanfootball_nfl` | `KXNFLGAME` | `KXNFLSPREAD` | `KXNFLTOTAL` |
| `americanfootball_ncaaf` | `KXNCAAFGAME` | `KXNCAAFSPREAD` | `KXNCAAFTOTAL` |
| `baseball_mlb` | `KXMLBGAME` | `KXMLBSPREAD` | `KXMLBTOTAL` |
| `basketball_nba` | `KXNBAGAME` | `KXNBASPREAD` | `KXNBATOTAL` |
| `basketball_ncaab` | `KXNCAAMBGAME` | `KXNCAAMBSPREAD` | `KXNCAAMBTOTAL` |
| `basketball_wnba` | `KXWNBAGAME` | `KXWNBASPREAD` | `KXWNBATOTAL` |
| `icehockey_nhl` | `KXNHLGAME` | `KXNHLSPREAD` | `KXNHLTOTAL` |
| `tennis_atp` | `KXATPMATCH` | — | — |
| `tennis_wta` | `KXWTAMATCH` | — | — |

Use `--kalshi-series` to override the mapped series.

### Why the Kalshi sides stay aligned

The finder does not infer a team from market order, event-title order, or a
ticker suffix. It:

1. fetches cursor-paginated Kalshi events with their nested markets;
2. resolves each market's `custom_strike` team UUID through
   `structured_targets`;
3. matches the unordered pair of participants to The Odds API's explicit home
   and away names within a configurable UTC kickoff window;
4. rejects non-unique or low-confidence assignments; and
5. carries the exact Kalshi ticker and `BUY YES` instruction into the result.

The finder deliberately uses only each team's direct YES proposition. It does
not assume that BUY NO on team A is equivalent to team B, because mutually
exclusive Kalshi markets are not necessarily exhaustive and settlement rules
can differ.

A Kalshi order book contains bids only. The current fixed-point response uses
`orderbook_fp.yes_dollars` and `orderbook_fp.no_dollars`; therefore a YES ask is
`$1 - best NO bid`, and a NO ask is `$1 - best YES bid`. The finder walks all
available levels for the requested payout, rejects insufficient depth, applies
the current series/event fee model, and conservatively rounds the resulting
cost.

NFL ties are included as a separate payoff scenario. By default, a sportsbook
two-way moneyline is modeled as a push and the Kalshi settlement value is read
from that market's rules. A combination with a negative tie payoff is rejected.
Change the sportsbook assumption with `--sportsbook-tie loss` if appropriate.

### Important limitations

- An output is a snapshot, not an execution guarantee. Prices and available
  size can change between API calls.
- Book limits, account-specific fee/rounding behavior, promotions, and taxes
  are not available from these feeds.
- Settlement, postponement, cancellation, and void rules can differ across
  venues. Read both contracts before trading even when every modeled payoff is
  nonnegative.
- Maker orders are intentionally excluded because a resting order is not an
  immediately executable comparison leg.

Current API references used by the implementation:

- [Kalshi API environments](https://docs.kalshi.com/getting_started/api_environments)
- [Kalshi events](https://docs.kalshi.com/api-reference/events/get-events)
- [Kalshi structured targets and milestones](https://docs.kalshi.com/getting_started/targets_and_milestones)
- [Kalshi order-book responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi series fee data](https://docs.kalshi.com/api-reference/market/get-series)
- [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [The Odds API v4](https://the-odds-api.com/liveapi/guides/v4/)
- [The Odds API betting-market keys](https://the-odds-api.com/sports-odds-data/betting-markets.html)
