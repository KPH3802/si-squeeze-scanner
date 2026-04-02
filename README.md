# SI Squeeze Scanner

Nightly scanner for **Short Interest Squeeze** signals. Monitors FINRA Consolidated Short Interest data (published twice monthly) for stocks with rapid short interest increases on small cap exchanges — a squeeze setup where shorts get trapped and the stock moves up.

## Signal Logic

The signal is **inverted**: a rapid *increase* in short interest on small cap exchanges indicates a squeeze setup — not a bearish signal.

- **Trigger**: Short interest increase ≥30% vs prior period on SC/exchange tickers
- **Direction**: LONG (BUY)
- **Hold**: 28 days (4 weeks)
- **Exclude**: OTC (penny stock noise destroys signal)
- **Data source**: FINRA Consolidated Short Interest API (free, no authentication required)
- **Cadence**: Published ~1st and ~15th of each month — scanner detects new settlement dates automatically

## Backtest Results

| Universe | Alpha (4w) | t-stat | Notes |
|----------|-----------|--------|-------|
| All exchanges | +2.62%/trade | — | Inverted signal confirmed |
| SC exchange only | +10.29%/trade | 30.47*** | **Deploy target** |

Data: Fintel/FINRA, 2018–2026. OTC excluded (penny stock blowups destroy signal).

## Architecture

```
si_scanner/
├── si_scanner.py       # Main scanner — runs nightly on PythonAnywhere
├── config.py           # Credentials and thresholds (not committed)
└── config_example.py   # Template — copy to config.py and fill in values
```

FINRA data is bi-monthly. The scanner probes for the latest settlement date each night and only processes new dates (deduplication via SQLite). On a new date: fetches all qualifying tickers, filters by price (≥$2), enriches with yfinance, emails signals.

Subject line parseable by IB AutoTrader:
- `SI SQUEEZE: AAPL, MSFT` → autotrader places BUY orders

## Setup

```bash
pip install requests yfinance
cp config_example.py config.py
# Edit config.py with your email credentials
python3 si_scanner.py --test-email    # Verify config
python3 si_scanner.py --dry-run       # Detect signals, skip email
python3 si_scanner.py --force         # Force re-run on latest date
python3 si_scanner.py --status        # Show DB stats
```

Deploy on PythonAnywhere: schedule `cd /home/KPH3802/si-scanner && python3 si_scanner.py` at **01:45 UTC** daily.

## IB AutoTrader Integration

The `ib-autotrader` repo parses SI Squeeze email subjects via `query_si_squeeze_signals_from_email()`. Signals are sized at full position ($5,000) and tracked in `positions.db` with:
- 28-day time exit
- -40% catastrophic circuit breaker

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `CHANGE_THRESHOLD` | 30.0 | % SI increase required for signal |
| `MIN_PRICE` | 2.0 | Minimum stock price (filters pennies) |
| `TARGET_MARKET_CLASSES` | SC, NNM, NYSE, AMEX, ARCA, BZX | Exchanges to include |
| `EXCLUDE_MARKET_CLASSES` | OTC, OTCBB, OC, PI | Exchanges to exclude |

## Disclaimer

For research and educational purposes. Not investment advice. Past backtest performance does not guarantee future results.

---

MIT License

