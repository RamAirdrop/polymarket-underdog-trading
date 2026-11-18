# Polymarket tennis tracker (patched + underdog mode + pre-match stack)

Run:  pip install -r requirements.txt  ->  python -m streamlit run app.py   (restart Streamlit fully after replacing files)

Strategies (tennis_core.py constants; chosen in the dashboard "Strategy" radio)
- underdog  : buys the CHEAPER side priced 15c-40c when  adjusted_p - (price + taker fee) >= 5 points. Stake 1% of bankroll.
- favourite : buys the model favourite priced 30c-80c with the same edge rule. Stake 2%.
- both      : whichever passes with the larger edge.
adjusted_p = 0.5*model + 0.5*market (the raw Elo model is ~3-5 pts overconfident on favourites).

Before risking money
1. python backtest.py --fetch 300   -> inspect data/backtest_v2.csv (p1+p2 ~ 1.0, never 0 or 1)
2. Dashboard > Backtest > run Underdog / Favourite and read the 95% CI. If it includes 0, there is no proven edge.
3. Paper trade 200+ bets; compare entry price with the price at match start.
4. Only then use real money, small stakes ($10-25). Never scale up after wins or chase losses.

Not tested against the live Polymarket API from the build environment: pre-match price history (backtest.pre_match_price),
real_buy order placement, and FEE_RATE (verify on Polymarket's fee page).


## Pre-match stack (strategy "stack", edge_stack.py)

Select "Stack (pre-match)" in the dashboard, or `python tennis_core.py scan --strategy stack`. Every rule is a constant at the top
of edge_stack.py. All thresholds are heuristics from a strategy write-up, NOT validated numbers - backtest and paper trade first.

1. Value (layer 1): the existing model (surface Elo + serve/return points, shrunk toward the market) must beat the entry price by 5 pts. 7+ pts = A-grade candidate.
2. Fatigue (layer 2): heavy fatigue on our pick (match >= 3h yesterday/today, or 3+ matches in 4 days) = VETO. Clear rest advantage (>= 2 more rest days than an opponent who played yesterday) = stake x 1.25.
   Unknown data is neutral: it never vetoes and never boosts. Only calendar days exist (no clock times), so "36 hours" is approximated as "yesterday".
3. Sharp line (layer 3): vig-free Pinnacle probability (env ODDS_API_KEY for The Odds API, or data/sharp_odds.csv with columns player1,player2,odds1,odds2).
   Sharp below our entry price = VETO. Blended into the probability (60% sharp / 40% model). No sharp line = grade C (smallest size) unless REQUIRE_SHARP = True.
4. Entry: resting limit order 1c above the best bid, never crossing the spread. Real orders are GTD post-only and expire at the scheduled start, so they cannot fill during the match. Paper orders fill when the ask trades down to your price.
5. Sizing: quarter-Kelly, capped by grade (A 3%, B 1.5%, C 0.75% of bankroll) and a 5% hard cap per match.
6. Liquidity gate: ask-side depth within 1c of the best ask must be >= 3x the order size.
7. Pre-match only: live matches always PASS (no averaging down, no in-play changes).

Tracking: Positions tab > Limit orders shows fill rate and CLV (closing-line value) for filled vs expired orders. CLV is sampled each time the page
refreshes, so refresh frequently before matches. `python paper.py orders` does the same from the CLI.

Backtest: `python backtest.py --fetch 300` now also builds fatigue flags and prints stack results with and without the fatigue layer.
The sharp-line layer has no historical data, and fatigue is only measured on matches with exact dates (after the match-history mirror ends), so the
stack backtest is a partial test. Run `python test_stack.py` (offline unit tests).

Not tested against the live exchange: real_limit_buy / order_status (live_trade.py), The Odds API fetch, maker fee assumption (MAKER_FEE_RATE = 0).


## Comparing strategies (paper)

Dashboard: turn on "Paper-trade EVERY strategy automatically" (or press "Run all strategies now") after locking today's stake.
Every strategy's BUY signals are paper-bought with the SAME flat stake; each strategy holds its own position per market.
Results appear in Positions > "Strategy comparison" (net of taker fee, hit rate, ROI with 95% interval, CLV).
CLI: `python paper.py autoall --stake 25 --loop 300` and `python paper.py compare`.
Decide only after ~200 settled bets per strategy and only if the ROI 95% low is above 0.

Each strategy trades its OWN paper account (default $1000 each, change it in Positions; "manual" keeps the old balance).
One strategy running out of cash never blocks the others. Positions tab shows a balance card per strategy, a cumulative
net-profit line chart (by date, or by bet number for an equal-sample comparison) and a verdict that will not call a winner
before ~200 settled bets or while the ROI range still includes zero. Wipe a single strategy from the Paper account expander.
