"""Dependency-free local browser UI for :mod:`arbitrage_finder`."""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import arbitrage_finder as finder


ODDS_CACHE = finder.OddsResponseCache(ttl_seconds=float("inf"))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Promo Odds Finder</title>
<style>
:root { --ink:#17212b; --muted:#65727f; --paper:#f4f1e9; --card:#fffdf8;
  --line:#d8d4c9; --accent:#146b5d; --accent2:#e7f3ee; --warn:#a14b18; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:var(--paper); font:15px/1.45 system-ui,sans-serif; }
main { max-width:1180px; margin:auto; padding:32px 20px 64px; }
h1 { margin:0; font:700 clamp(30px,5vw,52px)/1.05 Georgia,serif; letter-spacing:-.03em; }
.intro { color:var(--muted); max-width:720px; font-size:16px; }
.panel,.result { background:var(--card); border:1px solid var(--line); border-radius:16px;
  box-shadow:0 8px 28px #17212b0d; }
.panel { padding:20px; margin:24px 0; }
.panel legend { padding:0 8px; font-weight:800; color:var(--accent); }
.panel[disabled] { opacity:.5; }
.step-note { color:var(--muted); margin:0 0 14px; }
.grid { display:grid; grid-template-columns:repeat(4,minmax(150px,1fr)); gap:15px; }
label { display:block; font-weight:650; }
label span { display:block; color:var(--muted); font-size:12px; font-weight:500; margin-top:2px; }
input,select { width:100%; margin-top:6px; padding:10px 11px; border:1px solid var(--line);
  border-radius:9px; background:white; color:var(--ink); font:inherit; }
.field-title { font-weight:650; }
.field-title span { display:block; color:var(--muted); font-size:12px; font-weight:500; margin-top:2px; }
.multi { position:relative; margin-top:6px; }
.multi summary { list-style:none; padding:10px 11px; border:1px solid var(--line); border-radius:9px;
  background:white; cursor:pointer; font-weight:500; }
.multi summary::-webkit-details-marker { display:none; }
.multi summary::after { content:'▾'; float:right; color:var(--muted); }
.multi[open] summary::after { content:'▴'; }
.multi-options { position:absolute; z-index:10; top:calc(100% + 5px); left:0; right:0; max-height:280px;
  overflow:auto; padding:7px; background:white; border:1px solid var(--line); border-radius:10px;
  box-shadow:0 12px 28px #17212b24; }
.multi-options label { display:flex; align-items:center; gap:8px; padding:7px; border-radius:6px; font-weight:500; }
.multi-options label:hover { background:var(--accent2); }
.multi-options input { width:auto; margin:0; }
.checks { display:flex; flex-wrap:wrap; gap:18px; margin-top:18px; }
.checks label { display:flex; align-items:center; gap:7px; }
.checks input { width:auto; margin:0; }
button { margin-top:20px; border:0; border-radius:10px; padding:12px 20px; color:white;
  background:var(--accent); font:700 15px system-ui; cursor:pointer; }
button:disabled { opacity:.55; cursor:wait; }
.actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.secondary { color:var(--accent); background:var(--accent2); border:1px solid #a9d3c5; }
#status { color:var(--muted); margin:16px 2px; min-height:22px; }
.summary { display:flex; gap:10px; flex-wrap:wrap; margin:14px 0; }
.pill { border:1px solid var(--line); background:var(--card); padding:7px 11px; border-radius:999px; }
.result { padding:18px; margin:12px 0; }
.result-head { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; }
.match { font:700 20px/1.2 Georgia,serif; }
.time { color:var(--muted); font-size:13px; margin-top:4px; }
.kalshi-link { color:var(--accent); font-weight:700; text-decoration:none; }
.kalshi-link:hover { text-decoration:underline; }
.score { min-width:145px; text-align:right; }
.gap { color:var(--accent); font-size:24px; font-weight:800; }
.metrics { color:var(--muted); font-size:13px; }
.legs { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-top:14px; }
.leg { border:1px solid var(--line); border-radius:11px; padding:12px; }
.leg.low { background:var(--accent2); border-color:#a9d3c5; }
.selection { font-weight:750; }
.venue { color:var(--muted); }
.prob { font-size:20px; font-weight:800; margin-top:6px; }
.execution { font-size:12px; margin-top:5px; overflow-wrap:anywhere; }
.alternatives { margin-top:8px; padding-top:8px; border-top:1px dashed var(--line); color:var(--muted); font-size:12px; }
.tag { color:var(--accent); font-size:11px; font-weight:800; text-transform:uppercase; }
.error { color:#8b251d; background:#fff0ed; border:1px solid #efc0b8; padding:12px; border-radius:10px; }
details { color:var(--muted); margin-top:18px; }
@media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}.legs{grid-template-columns:1fr}}
@media(max-width:480px){.grid{grid-template-columns:1fr}.result-head{display:block}.score{text-align:left;margin-top:10px}}
</style>
</head>
<body><main>
<h1>Promo Odds Finder</h1>
<p class="intro">Find opposing moneyline legs whose implied probabilities land closest to 100%.
Lower-probability legs are highlighted as natural promo-bet candidates. Kalshi prices include visible depth and modeled taker fees.</p>
<section class="panel" id="scopePanel">
  <div class="tag">Step 1</div>
  <h2>Choose sportsbook scope</h2>
  <p class="step-note">These settings determine which sportsbook data the API must fetch. Changing any of them starts a new snapshot.</p>
  <div class="grid">
    <label>Sport<select id="sport">SPORT_OPTIONS</select></label>
    <label>Regions<input id="regions" value="us,us2"></label>
    <div><div class="field-title">Bookmakers<span>Select any number of books</span></div>
      <details class="multi" id="bookmakerMenu"><summary id="bookmakerSummary">All bookmakers</summary>
        <div class="multi-options" id="bookmakerOptions">
          <label><input class="bookmaker-all" type="checkbox" checked> All bookmakers</label>
          <label><input class="bookmaker" type="checkbox" value="draftkings"> DraftKings</label>
          <label><input class="bookmaker" type="checkbox" value="fanduel"> FanDuel</label>
          <label><input class="bookmaker" type="checkbox" value="betmgm"> BetMGM</label>
          <label><input class="bookmaker" type="checkbox" value="williamhill_us"> Caesars</label>
          <label><input class="bookmaker" type="checkbox" value="espnbet"> theScore Bet / ESPN BET</label>
          <label><input class="bookmaker" type="checkbox" value="betrivers"> BetRivers</label>
          <label><input class="bookmaker" type="checkbox" value="hardrockbet"> Hard Rock Bet</label>
          <label><input class="bookmaker" type="checkbox" value="betparx"> betPARX</label>
          <label><input class="bookmaker" type="checkbox" value="ballybet"> Bally Bet</label>
          <label><input class="bookmaker" type="checkbox" value="bovada"> Bovada</label>
          <label><input class="bookmaker" type="checkbox" value="betonlineag"> BetOnline.ag</label>
          <label><input class="bookmaker" type="checkbox" value="betus"> BetUS</label>
          <label><input class="bookmaker" type="checkbox" value="mybookieag"> MyBookie.ag</label>
          <label><input class="bookmaker" type="checkbox" value="lowvig"> LowVig.ag</label>
          <label><input class="bookmaker" type="checkbox" value="betanysports"> BetAnySports</label>
        </div>
      </details>
    </div>
    <label>Upcoming hours<input id="hours" type="number" value="168" min="1" step="1"></label>
  </div>
  <div class="checks">
    <strong>Markets:</strong>
    <label><input class="scope-market" type="checkbox" value="h2h" checked> Moneyline</label>
    <label id="spreadMarketLabel"><input id="spreadMarket" class="scope-market" type="checkbox" value="spreads"> Spreads</label>
    <label id="totalMarketLabel"><input id="totalMarket" class="scope-market" type="checkbox" value="totals"> Totals</label>
    <label id="tdMarketLabel"><input id="tdMarket" class="scope-market" type="checkbox" value="player_anytime_td"> NFL anytime TD</label>
  </div>
  <div class="checks">
    <label><input id="kalshi" type="checkbox" checked> Include Kalshi</label>
    <label><input id="live" type="checkbox"> Include recently started games</label>
  </div>
  <button id="continue" type="button">Load market snapshot</button>
</section>
<fieldset class="panel" id="analysisFilters" disabled>
  <legend>Step 2 · Analysis filters</legend>
  <p class="step-note">These controls reanalyze the loaded snapshot without contacting the external APIs. Refresh only when you need new prices.</p>
  <div class="grid">
    <label>Maximum vig gap (%)<span>Distance from a 100% implied sum</span><input id="maxVig" type="number" value="5" min="0" step="0.1"></label>
    <label>Maximum low-leg probability (%)<span>Use 100 to show every pair</span><input id="maxLow" type="number" value="100" min="0" max="100" step="1"></label>
    <label>Pricing payout ($)<span>Used for Kalshi depth and fees</span><input id="payout" type="number" value="100" min="1" step="1"></label>
    <label>Near misses<input id="nearMisses" type="number" value="5" min="0" max="50" step="1"></label>
  </div>
  <div class="checks">
    <strong>Show markets:</strong>
    <label><input class="filter-market" type="checkbox" value="h2h" checked> Moneyline</label>
    <label><input class="filter-market" type="checkbox" value="spreads"> Spreads</label>
    <label><input class="filter-market" type="checkbox" value="totals"> Totals</label>
    <label><input class="filter-market" type="checkbox" value="player_anytime_td"> NFL anytime TD</label>
  </div>
  <div class="checks">
    <label><input id="filterKalshi" type="checkbox" checked> Include Kalshi</label>
    <label><input id="requireKalshi" type="checkbox"> Require a Kalshi leg</label>
  </div>
  <div class="actions">
    <button id="find" type="button">Apply filters</button>
    <button id="refresh" class="secondary" type="button">Refresh prices</button>
  </div>
</fieldset>
<div id="status"></div><div id="results"></div>
</main>
<script>
const $=id=>document.getElementById(id), pct=x=>(Number(x)*100).toFixed(2)+'%', price=x=>'$'+Number(x).toFixed(2), volume=x=>Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
const h=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function legText(leg){
  if(leg.source_type==='kalshi') return `BUY ${h(leg.side)} · ${h(leg.market_ticker)} · avg $${h(leg.average_price)} · fee $${h(leg.fee)}`;
  return `American ${h(leg.american_odds)} · ${h(leg.bookmaker_key)}`;
}
function renderCandidate(c, rejected=false){
  const e=c.event, low=Number(c.low_probability);
  const kalshiLink=e.kalshi_url?` · <a class="kalshi-link" href="${h(e.kalshi_url)}" target="_blank" rel="noopener noreferrer">Open on Kalshi ↗</a>`:'';
  const legs=c.legs.map(l=>{const p=Number(c.implied_probabilities[l.selection]);
    const nearby=(l.similar_sportsbooks||[]).map(x=>`${h(x.venue)} <strong>${h(x.american_odds)}</strong>`).join(' · ');
    const kalshiDepth=l.source_type==='kalshi'&&l.current_price!=null?`<div class="alternatives"><strong>Kalshi order book</strong><br>Current ask <strong>${price(l.current_price)}</strong> · ${pct(l.current_price_implied_probability)} implied with taker fee · volume ${volume(l.current_price_volume)}<br>Bid (1¢ lower) <strong>${price(l.one_cent_lower_bid_price)}</strong> · ${pct(l.one_cent_lower_bid_implied_probability)} implied with maker fee · volume ${volume(l.one_cent_lower_bid_volume)}</div>`:'';
    return `
    <div class="leg ${Math.abs(p-low)<1e-10?'low':''}">
      ${Math.abs(p-low)<1e-10?'<div class="tag">Lower-probability leg</div>':''}
      <div class="selection">${h(l.selection)}</div><div class="venue">${h(l.venue)}</div>
      <div class="prob">${pct(p)} implied</div><div class="execution">${legText(l)}</div>
      ${kalshiDepth}
      ${nearby?`<div class="alternatives"><strong>Similar books within 1pp</strong><br>${nearby}</div>`:''}
    </div>`}).join('');
  return `<article class="result"><div class="result-head"><div><div class="tag">${h(e.market_label)}</div><div class="match">${h(e.away_team)} at ${h(e.home_team)}</div>
    <div class="time">${h(new Date(e.commence_time).toLocaleString())}${e.kalshi_event_ticker?' · '+h(e.kalshi_event_ticker):''}${kalshiLink}</div></div>
    <div class="score"><div class="gap">${pct(c.vig)} vig</div><div class="metrics">sum ${pct(c.implied_probability_sum)} · distance ${pct(c.vig_gap)}</div></div></div>
    <div class="legs">${legs}</div>${rejected?`<div class="metrics" style="margin-top:9px">Filtered: ${h(c.rejection_reason)}</div>`:''}</article>`;
}
function selectedBookmakers(){
  if(document.querySelector('.bookmaker-all').checked) return '';
  return [...document.querySelectorAll('.bookmaker:checked')].map(x=>x.value).join(',');
}
function syncBookmakers(source){
  const all=document.querySelector('.bookmaker-all'), books=[...document.querySelectorAll('.bookmaker')];
  if(source===all && all.checked) books.forEach(x=>x.checked=false);
  if(source!==all && source?.checked) all.checked=false;
  if(!all.checked && !books.some(x=>x.checked)) all.checked=true;
  const selected=books.filter(x=>x.checked);
  $('bookmakerSummary').textContent=all.checked?'All bookmakers':`${selected.length} bookmaker${selected.length===1?'':'s'} selected`;
}
function syncAnalysisScope(){
  const loadedMarkets=new Set([...document.querySelectorAll('.scope-market:checked')].map(x=>x.value));
  document.querySelectorAll('.filter-market').forEach(x=>{x.disabled=!loadedMarkets.has(x.value);x.checked=loadedMarkets.has(x.value)});
  $('filterKalshi').disabled=!$('kalshi').checked;
  $('filterKalshi').checked=$('kalshi').checked;
  $('requireKalshi').disabled=!$('kalshi').checked;
  if(!$('kalshi').checked)$('requireKalshi').checked=false;
}
function unlockAnalysis(){
  $('analysisFilters').disabled=false;
  $('analysisFilters').scrollIntoView({behavior:'smooth',block:'start'});
}
function lockAnalysis(){
  $('analysisFilters').disabled=true;
  $('results').innerHTML='';
  $('status').textContent='Scope changed. Continue to analysis filters when ready.';
}
function renderResults(data){
  const shownMarkets=new Set([...document.querySelectorAll('.filter-market:checked')].map(x=>x.value));
  const showCandidate=c=>shownMarkets.has(c.event.market_key)&&($('filterKalshi').checked||!c.uses_kalshi);
  const opportunities=data.opportunities.filter(showCandidate), nearMisses=(data.near_misses||[]).filter(showCandidate);
  const c=data.counts, quota=data.quota||{};
  $('status').innerHTML=`<div class="summary"><span class="pill">${opportunities.length} close pairs</span><span class="pill">${c.odds_events} sportsbook markets loaded</span><span class="pill">${c.matched_events} Kalshi matches</span>${quota['x-snapshot-cache']==='reused'?'<span class="pill">Market snapshot reused</span>':''}${quota['x-requests-remaining']?`<span class="pill">${h(quota['x-requests-remaining'])} API requests left</span>`:''}</div>`;
  let html=opportunities.map(c=>renderCandidate(c)).join('');
  if(!html) html='<div class="panel">No pairs meet the current filters. Try increasing the maximum vig gap or low-leg probability.</div>';
  if(nearMisses.length) html+=`<details><summary>Show ${nearMisses.length} closest filtered pairs</summary>${nearMisses.map(c=>renderCandidate(c,true)).join('')}</details>`;
  $('results').innerHTML=html;
}
async function run(refreshPrices=false, loadScope=false){
  const buttons=[$('continue'),$('find'),$('refresh')]; buttons.forEach(x=>x.disabled=true); $('status').textContent=refreshPrices?'Refreshing current prices…':'Loading market snapshot…'; $('results').innerHTML='';
  const body={sport:$('sport').value,max_vig:Number($('maxVig').value),max_low_probability:Number($('maxLow').value),
    hours_ahead:Number($('hours').value),regions:$('regions').value,bookmakers:selectedBookmakers(),payout:Number($('payout').value),
    markets:[...document.querySelectorAll('.scope-market:checked')].map(x=>x.value),
    include_kalshi:loadScope?$('kalshi').checked:$('filterKalshi').checked,require_kalshi:loadScope?false:$('requireKalshi').checked,include_live:$('live').checked,
    near_misses:Number($('nearMisses').value),refresh_prices:refreshPrices||loadScope};
  try { const response=await fetch('/api/find',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json(); if(!response.ok) throw new Error(data.error||'Request failed');
    if(loadScope){syncAnalysisScope();unlockAnalysis()}
    renderResults(data);
  } catch(err){$('status').innerHTML=`<div class="error">${h(err.message)}</div>`} finally {buttons.forEach(x=>x.disabled=false)}
}
$('continue').addEventListener('click',()=>run(false,true));
$('find').addEventListener('click',()=>run(false,false));
$('refresh').addEventListener('click',()=>run(true,true));
$('kalshi').addEventListener('change',()=>{if(!$('kalshi').checked)$('requireKalshi').checked=false;lockAnalysis()});
$('filterKalshi').addEventListener('change',()=>{const enabled=$('filterKalshi').checked;$('requireKalshi').disabled=!enabled;if(!enabled)$('requireKalshi').checked=false});
document.querySelectorAll('.bookmaker,.bookmaker-all').forEach(x=>x.addEventListener('change',()=>{syncBookmakers(x);lockAnalysis()}));
document.querySelectorAll('.scope-market,#live').forEach(x=>x.addEventListener('change',lockAnalysis));
function syncSportMarkets(){
  const sport=$('sport').value, nfl=sport==='americanfootball_nfl', tennis=sport.startsWith('tennis_');
  $('tdMarket').disabled=!nfl;
  if(!nfl)$('tdMarket').checked=false; $('tdMarketLabel').style.opacity=nfl?'1':'.45';
  for(const [input,label] of [[$('spreadMarket'),$('spreadMarketLabel')],[$('totalMarket'),$('totalMarketLabel')]]){
    input.disabled=tennis; if(tennis)input.checked=false; label.style.opacity=tennis?'.45':'1';
  }
}
$('sport').addEventListener('change',()=>{syncSportMarkets();lockAnalysis()});
$('regions').addEventListener('input',lockAnalysis);
$('hours').addEventListener('input',lockAnalysis);
syncSportMarkets();
</script></body></html>"""


def _number(payload: dict[str, Any], key: str, default: str) -> Decimal:
    return finder.decimal_value(payload.get(key, default), field_name=key)


def _config(payload: dict[str, Any]) -> finder.RunConfig:
    sport_key = str(payload.get("sport", "americanfootball_ncaaf"))
    if sport_key not in finder.SPORTS:
        raise finder.FinderError(f"Unsupported sport: {sport_key}")
    max_vig = _number(payload, "max_vig", "5")
    max_low = _number(payload, "max_low_probability", "100")
    hours = _number(payload, "hours_ahead", "168")
    payout = _number(payload, "payout", "100")
    if max_vig < 0 or not 0 <= max_low <= 100 or hours <= 0 or payout <= 0:
        raise finder.FinderError("Check the numeric filters; one is outside its allowed range")
    use_kalshi = bool(payload.get("include_kalshi", True))
    require_kalshi = bool(payload.get("require_kalshi", False))
    if require_kalshi and not use_kalshi:
        raise finder.FinderError("Require Kalshi cannot be used when Kalshi is disabled")
    sport = finder.SPORTS[sport_key]
    market_value = payload.get("markets", ["h2h"])
    if isinstance(market_value, list):
        markets = tuple(str(value) for value in market_value if value)
    else:
        markets = finder.comma_values(str(market_value))
    allowed_markets = {"h2h", "spreads", "totals", "player_anytime_td"}
    if not markets or set(markets) - allowed_markets:
        raise finder.FinderError("Select at least one supported market")
    if "player_anytime_td" in markets and sport_key != "americanfootball_nfl":
        raise finder.FinderError("Anytime touchdown is available only for NFL")
    if sport_key.startswith("tennis_") and set(markets) != {"h2h"}:
        raise finder.FinderError("Tennis currently supports moneyline only")
    return finder.RunConfig(
        sport=sport,
        kalshi_series=sport.kalshi_series,
        regions=finder.comma_values(str(payload.get("regions", "us,us2"))),
        bookmakers=finder.comma_values(str(payload.get("bookmakers", ""))),
        payout=payout,
        minimum_profit=Decimal("0"),
        minimum_roi=Decimal("0"),
        start_tolerance=timedelta(minutes=180),
        hours_ahead=hours,
        include_live=bool(payload.get("include_live", False)),
        max_quote_age=None,
        sportsbook_stake_increment=Decimal("0.01"),
        kalshi_balance_precision=Decimal("0.0001"),
        sportsbook_tie_mode="push",
        use_kalshi=use_kalshi,
        require_kalshi=require_kalshi,
        timeout=15.0,
        retries=3,
        workers=8,
        maximum_vig=max_vig / Decimal("100"),
        max_low_probability=(max_low / Decimal("100") if max_low < 100 else None),
        markets=markets,
    )


class UiHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/index.html"):
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        options = "".join(
            f'<option value="{key}"'
            f'{" selected" if key == "americanfootball_ncaaf" else ""}>'
            f'{spec.label}</option>'
            for key, spec in finder.SPORTS.items()
        )
        body = PAGE.replace("SPORT_OPTIONS", options).encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/find":
            self._send(404, b'{"error":"Not found"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100_000:
                raise finder.FinderError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise finder.FinderError("Expected a JSON object")
            if payload.get("refresh_prices") is True:
                ODDS_CACHE.clear()
            report = finder.run_finder(
                _config(payload),
                os.environ.get("THE_ODDS_API_KEY", "").strip(),
                odds_cache=ODDS_CACHE,
            )
            near = max(0, min(50, int(payload.get("near_misses", 5))))
            result = finder.report_dict(report, include_rejected=near)
            body = json.dumps(result).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        except (finder.FinderError, ValueError, TypeError, json.JSONDecodeError) as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self._send(400, body, "application/json; charset=utf-8")
        except Exception:
            body = b'{"error":"Unexpected server error; see the terminal for details"}'
            self._send(500, body, "application/json; charset=utf-8")
            raise

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"UI: {fmt % args}")


def launch_ui(
    *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True
) -> None:
    """Serve the local dashboard until Ctrl+C."""

    finder.load_env_file()
    server = ThreadingHTTPServer((host, port), UiHandler)
    url = f"http://{host}:{server.server_port}/"
    print(f"Promo Odds Finder is running at {url}")
    print("Press Ctrl+C to stop it.")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    launch_ui()
