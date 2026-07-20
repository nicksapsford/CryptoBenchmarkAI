# CryptoBenchmark A.I.

Part of the **Albion Benchmark Desk** — a parallel scientific baseline for the
original Albion Trading Desk. CryptoBenchmark trades BTC + ETH on **pure Lancelot
signals with no AI overlay**, so its P&L can be compared like-for-like against the
original CryptoTrader.

- **Port:** 5021  ·  **Instruments:** BTC + ETH (Kraken, 24/7)  ·  **Balance:** £2,000 (£1k BTC + £1k ETH)
- **Template:** CryptoTrader v1.7.5  ·  **Paper trading only**

## Decision engine (the whole thing)

Every 5-minute candle, per instrument:

1. **Lancelot pre-checks** must all pass — identical to CryptoTrader (`pre_checks_btc` / `pre_checks_eth`).
2. **3-timeframe SSL agreement** — Daily + 1h + 5m SSL must all point the same way. That is the direction signal.
3. **Direction switch** decides execution:
   - `WITH` — trade the SSL direction.
   - `AGAINST` — trade the opposite (contrarian). Lancelot still validates the *signal*; only the executed direction flips.

Lancelot clears + 3-TF SSL agrees + switch → Stanley executes. Nothing else.
Exits are pure risk management: **1% trailing stop / 2% take profit / Profit Protection Ladder (Variant 2)**.

**Stripped vs CryptoTrader:** no Arthur (AI), no Morgan (confidence), no Guinevere
(news), no phantom logging, no regime/whale overlay.

## The direction switch (WITH / AGAINST)

- One switch for **both** BTC and ETH.
- **Live reload** — the engine re-reads `logs/direction_switch.json` on every tick; flipping it from the dashboard takes effect with **no restart**.
- Default **WITH**; state persists across restarts; written atomically.

## Running

```
python dashboard_cryptobenchmark.py     # port 5021 (switch control + status)
python watchdog_cryptobenchmark.py      # supervises main_cryptobenchmark.py
```

## Layout

| File | Role |
|---|---|
| `main_cryptobenchmark.py` | engine (Lancelot + 3-TF SSL + switch) |
| `dashboard_cryptobenchmark.py` | port 5021 dashboard + `/api/direction` |
| `watchdog_cryptobenchmark.py` | Galahad supervisor |
| `direction_switch.py` | WITH/AGAINST live-reload switch |
| `data_feed_*`, `pre_checks_*`, `strategy_*`, `paper_trader_*`, `notifier_btc` | reused verbatim from CryptoTrader |

All times UTC.
