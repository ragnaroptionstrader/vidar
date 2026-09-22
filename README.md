# VIDAR — Short Premium Index Book

**VIDAR is the operator's overhaul primary book launched 2026-09-22.**

The volatility risk premium (VRP) — implied vol persistently above realized vol — is the most documented structural edge in US equity options markets. VIDAR captures it systematically.

**Strategy:** Sell 30-45 DTE iron condors on SPY (SPX alternative), 16-delta short wings, 5-wide wings for SPY ($500 max risk per contract), 50% profit-take, 2x credit stop, DTE-7 close.

**Why:** Carr & Wu (2009) "Variance Risk Premiums" — VRP averages ~2 vol points annualized, ~8-12% systematic return, Sharpe 0.4-0.7. Coval & Shumway (2005) — delta-hedged ATM straddle on S&P 500 has expected return ~−10%/yr, selling it captures Sharpe 0.4-0.5.

**Why SPY not SPX:** SPY has $5-wide strikes (SPX is $5 too but settlement different). SPY is equity-settled, smaller notional, fits the $750 envelope with 1 contract (~$350 max risk).

**Expected return:** 8-12% annualized gross, net of $1/contract commissions and 1-2% slippage = 6-10% net.

**Risk:**
- Max loss per trade: $500 (1 SPY $5-wide IC)
- Max open positions: 1
- Max daily loss: $500
- Expected max DD over 12 months: 15-25% normal; 25-35% in crash regimes (2008, 2020 analog)

**Layout:**
```
vidar/
├── README.md
├── pyproject.toml
├── cli/scan.py                     # Daily scanner: SPY 30-45 DTE 16-delta IC
├── vidar/
│   ├── audit.py                    # JSONL writer, strategy=vidar tag
│   ├── sizing.py                   # Fixed-fractional 1 contract
│   ├── exits/ladder.py             # 50% profit, 2x stop, DTE-7
│   ├── execution/paper.py          # TigerBroker wrapper
│   └── signals/vrp.py              # VRP scoring: IV vs RV, term structure
├── ragnar_scripts/vidar_auto.py     # phase orchestrator (build/open/exit)
└── tests/                          # unit tests
```

**Status (2026-09-22):** scaffold + scanner + paper-trade path. Live arming pending 2-week paper-trade validation.
