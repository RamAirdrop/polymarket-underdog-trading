# Polymarket tennis tracker (patched + underdog mode)

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
