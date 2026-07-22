"""
main_cryptobenchmark.py -- CryptoBenchmark A.I. engine (Benchmark Desk)
================================================================================
A parallel *scientific baseline* for CryptoTrader. NO Arthur (AI), NO Morgan
(confidence), NO Guinevere (news), NO phantom logging, NO regime/whale overlay.

The entire decision engine, per instrument (BTC and ETH), every 5m candle:

  STEP 1  All Lancelot pre-checks must pass       (identical to CryptoTrader)
  STEP 2  Daily + 1h + 5m SSL must ALL agree      (the direction signal)
  STEP 3  Direction switch decides execution:
            WITH    -> trade the SSL direction
            AGAINST -> trade the opposite (contrarian)

Lancelot clears + 3-TF SSL agrees + switch => Stanley executes. Nothing else.
Exits are pure risk management -- 1% trailing stop / 2% take profit / Profit
Protection Ladder (Variant 2) -- via the Trade object, so an AGAINST trade is
never force-closed by the template's own 1h-trend-reversal rule.

24/7 (Kraken). Port 5021. £1,000 BTC + £1,000 ETH = £2,000. All times UTC.
Template: CryptoTrader v1.7.5.
"""
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from data_feed_btc import BTCDataFeed, fetch_latest_1m_bar
from data_feed_eth import ETHDataFeed, fetch_latest_1m_bar as eth_fetch_latest_1m_bar
from paper_trader_btc import PaperTrader
from paper_trader_eth import PaperTraderETH
from pre_checks_btc import run_all_pre_checks as btc_pre_checks
from pre_checks_eth import run_all_pre_checks as eth_pre_checks
import notifier_btc as notifier
import direction_switch

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

_VER = BASE_DIR / "VERSION"
VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG = LOG_DIR / "shutdown.flag"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logging.Formatter.converter = time.gmtime      # all log timestamps UTC
log = logging.getLogger("CryptoBenchmark")

PORT              = 5021
DASHBOARD_URL     = "http://localhost:%d/api/update" % PORT
CANDLE_SECONDS    = 300      # 5-minute candle cadence
MONITOR_SECONDS   = 30       # in-trade price monitor cadence

_SHUTDOWN = False


def _handle_signal(sig, frame):
    global _SHUTDOWN
    _SHUTDOWN = True
    log.info("Signal %s received -- shutting down", sig)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Account (dict, as the Lancelot pre-checks expect) ─────────────────────────

def fresh_account() -> dict:
    return {
        "killed": False, "daily_pnl_gbp": 0.0, "consecutive_losses": 0,
        "last_loss_time": None, "kill_history": [], "kill_tier": 0,
        "kill_reason": "", "kill_time": None, "kill_wait_hours": 0, "kill_last_log": 0.0,
    }


def record_account(account: dict, pnl_gbp: float) -> None:
    account["daily_pnl_gbp"] = round(account.get("daily_pnl_gbp", 0.0) + pnl_gbp, 2)
    if pnl_gbp < 0:
        account["consecutive_losses"] = account.get("consecutive_losses", 0) + 1
        account["last_loss_time"] = datetime.now(timezone.utc)
    else:
        account["consecutive_losses"] = 0


def reset_daily_account(account: dict) -> None:
    """Benchmark daily-reset fix (22 Jul 2026). Clear the daily loss tally and the
    daily-loss kill switch on a new UTC trading day so yesterday's loss does not carry
    into today. Previously daily_pnl_gbp was zeroed ONLY at process start, so a prior
    day's loss permanently re-triggered the kill switch until a manual restart."""
    account["daily_pnl_gbp"] = 0.0
    account["consecutive_losses"] = 0
    account["killed"] = False
    account["kill_tier"] = 0
    account["kill_reason"] = ""
    account["kill_time"] = None
    account["kill_wait_hours"] = 0


# ── SSL 3-timeframe agreement (the benchmark's direction signal) ──────────────

def _ssl(bar):
    """'LONG'/'SHORT'/None from a bar's ssl_bull flag."""
    if bar is None:
        return None
    v = bar.get("ssl_bull")
    try:
        import pandas as pd
        if v is None or pd.isna(v):
            return None
    except Exception:
        if v is None:
            return None
    return "LONG" if bool(v) else "SHORT"


def ssl_agreement(bar_1d, bar_1h, bar_5m):
    """Daily + 1h + 5m SSL must ALL agree. Returns 'LONG'/'SHORT' or None."""
    d, h, m = _ssl(bar_1d), _ssl(bar_1h), _ssl(bar_5m)
    if d is not None and d == h == m:
        return d
    return None


# ── Per-instrument container ──────────────────────────────────────────────────

class Instrument:
    def __init__(self, label, feed, trader, pre_check_fn, live_price_fn):
        self.label = label
        self.feed = feed
        self.trader = trader
        self.pre_check_fn = pre_check_fn
        self.live_price_fn = live_price_fn
        self.account = fresh_account()
        self.last_checks = {"passed": None, "reason": "awaiting first tick"}
        self.last_signal = None

    @property
    def strategy(self):
        return self.trader.strategy


def _live_price(inst, bar_5m):
    try:
        b = inst.live_price_fn()
        if b is not None:
            px = float(b["close"] if isinstance(b, dict) or hasattr(b, "__getitem__") else b)
            if px > 0:
                return px
    except Exception:
        pass
    return float(bar_5m["close"])


# ── Position monitoring (pure risk management: ladder / trailing / stop-target) ─

def monitor_position(inst, price) -> bool:
    """Manage an open trade. Returns True if it was closed this call."""
    strat = inst.strategy
    trade = strat.current_trade
    if trade is None:
        return False
    try:
        trade.apply_profit_ladder(price)
    except Exception as exc:
        log.warning("[%s] ladder update failed: %s", inst.label, exc)
    trade.update_trailing_stop(price)
    reason = trade.check_exit(price)
    if not reason:
        return False
    direction = trade.direction
    strat._close_trade(price, reason)                 # updates capital, appends history
    closed = strat.trade_history[-1] if strat.trade_history else None
    if closed is not None:
        try:
            inst.trader._log_trade(closed)             # persist to trades.csv
            inst.trader._save_summary()
        except Exception as exc:
            log.warning("[%s] trade persist failed: %s", inst.label, exc)
        record_account(inst.account, closed.pnl_gbp)
        pnl = closed.pnl_gbp
        pnl_pct = getattr(closed, "pnl_pct", None) or 0.0
        try:
            if pnl >= 0:
                notifier.notify_trade_closed_win(direction, price, pnl, pnl_pct,
                                                 strat.capital_gbp, reason, pair=inst.label)
            else:
                notifier.notify_trade_closed_loss(direction, price, pnl, pnl_pct,
                                                  strat.capital_gbp, reason, pair=inst.label)
        except Exception as exc:
            log.warning("[%s] Percival close notify failed: %s", inst.label, exc)
        log.info("[%s] CLOSED %s @ %.2f | %s | P&L=£%+.2f | capital=£%.2f",
                 inst.label, direction, price, reason, pnl, strat.capital_gbp)
    return True


# ── One instrument candle tick: STEP 1 Lancelot -> STEP 2 SSL -> STEP 3 switch ─

def instrument_tick(inst, btc_atr):
    feed = inst.feed
    feed.refresh()
    try:
        bar_1d = feed.latest_bar("1d")
    except Exception:
        bar_1d = None
    bar_1h = feed.latest_bar("1h")
    bar_5m = feed.latest_bar("5m")
    price = _live_price(inst, bar_5m)
    strat = inst.strategy

    if strat.in_trade:
        monitor_position(inst, price)
        return

    # ---- Flat: look for a benchmark entry ----
    checks = inst.pre_check_fn(bar_1h, bar_5m, inst.account, None, bar_1d, btc_atr)
    inst.last_checks = checks
    signal_dir = ssl_agreement(bar_1d, bar_1h, bar_5m)
    inst.last_signal = signal_dir

    if not checks.get("passed"):
        log.info("[%s] Lancelot BLOCK: %s", inst.label, checks.get("reason"))
        return
    if signal_dir is None:
        log.info("[%s] No 3-TF SSL agreement -- no trade", inst.label)
        return

    mode = direction_switch.get_mode()                        # live reload each tick
    exec_dir = signal_dir if mode == "WITH" else direction_switch.flip(signal_dir)
    strat._open_trade(exec_dir, price)
    trade = strat.current_trade
    try:
        inst.trader._save_summary()
    except Exception:
        pass
    try:
        notifier.notify_trade_opened(exec_dir, trade.entry_price, trade.stop_loss,
                                     trade.take_profit, trade.position_size_gbp, pair=inst.label)
    except Exception as exc:
        log.warning("[%s] Percival open notify failed: %s", inst.label, exc)
    log.info("[%s] OPEN %s (signal %s, switch %s) @ £%.2f | stop=£%.2f target=£%.2f",
             inst.label, exec_dir, signal_dir, mode, trade.entry_price,
             trade.stop_loss, trade.take_profit)


# ── Dashboard state push ──────────────────────────────────────────────────────

def _inst_state(inst, price):
    strat = inst.strategy
    trade = strat.current_trade
    pos = None
    floating = 0.0
    locked = None
    if trade is not None:
        try:
            if trade.direction == "LONG":
                floating = trade.position_size_gbp * ((price - trade.entry_price) / trade.entry_price)
            else:
                floating = trade.position_size_gbp * ((trade.entry_price - price) / trade.entry_price)
        except Exception:
            floating = 0.0
        lf = getattr(trade, "ladder_floor_gbp", 0.0) or 0.0
        locked = round(lf, 2) if lf > 0 else None
        pos = {"direction": trade.direction, "entry": round(trade.entry_price, 2),
               "stop": round(trade.stop_loss, 2), "target": round(trade.take_profit, 2),
               "size_gbp": round(trade.position_size_gbp, 2),
               "floating_gbp": round(floating, 2), "locked_gbp": locked}
    lc = inst.last_checks or {}
    lanc = "CLEAR" if lc.get("passed") else ("BLOCKED: " + str(lc.get("reason") or "--"))
    if strat.in_trade:
        lanc = "IN TRADE"
    return {
        "label": inst.label,
        "price": round(price, 2),
        "in_trade": strat.in_trade,
        "position": pos,
        "floating_gbp": round(floating, 2),
        "locked_gbp": locked,
        "balance": round(strat.capital_gbp, 2),
        "today_pnl": round(inst.account.get("daily_pnl_gbp", 0.0), 2),
        "signal": inst.last_signal or "--",
        "lancelot": lanc,
    }


def push_dashboard(btc, eth, mode):
    try:
        b5 = btc.feed.latest_bar("5m"); e5 = eth.feed.latest_bar("5m")
        bpx = _live_price(btc, b5); epx = _live_price(eth, e5)
    except Exception:
        return
    bstate = _inst_state(btc, bpx); estate = _inst_state(eth, epx)
    payload = {
        "system": "CryptoBenchmark", "version": VERSION, "port": PORT,
        "mode": mode, "session": "24/7 -- Always Active",
        "updated_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "btc": bstate, "eth": estate,
        "portfolio": {
            "balance": round(bstate["balance"] + estate["balance"], 2),
            "today_pnl": round(bstate["today_pnl"] + estate["today_pnl"], 2),
            "floating_gbp": round(bstate["floating_gbp"] + estate["floating_gbp"], 2),
        },
    }
    try:
        requests.post(DASHBOARD_URL, json=payload, timeout=3)
    except Exception:
        pass   # dashboard may not be running -- engine keeps trading


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 70)
    log.info("  CryptoBenchmark A.I. v%s  (Benchmark Desk, port %d)", VERSION, PORT)
    log.info("  BTC + ETH | Kraken 24/7 | Pure Lancelot + 3-TF SSL + WITH/AGAINST")
    log.info("  Mode: %s | PAPER TRADING", direction_switch.get_mode())
    log.info("=" * 70)

    btc_feed = BTCDataFeed()
    eth_feed = ETHDataFeed()
    try:
        btc_feed.initialise()
    except Exception as exc:
        log.error("FATAL: BTC feed init failed: %s", exc); sys.exit(1)
    try:
        eth_feed.initialise()
    except Exception as exc:
        log.error("FATAL: ETH feed init failed: %s", exc); sys.exit(1)

    btc = Instrument("BTC", btc_feed, PaperTrader(), btc_pre_checks, fetch_latest_1m_bar)
    eth = Instrument("ETH", eth_feed, PaperTraderETH(), eth_pre_checks, eth_fetch_latest_1m_bar)

    try:
        notifier.notify_system_startup(btc.strategy.capital_gbp + eth.strategy.capital_gbp, pair="CryptoBenchmark")
    except Exception:
        pass
    SHUTDOWN_FLAG.unlink(missing_ok=True)

    log.info("Running. Dashboard: http://localhost:%d (start dashboard separately).", PORT)
    last_candle = 0.0
    last_monitor = 0.0
    last_push = 0.0
    current_day = datetime.now(timezone.utc).date()  # Benchmark daily-reset fix

    while not _SHUTDOWN:
        try:
            if SHUTDOWN_FLAG.exists():
                log.info("Shutdown flag seen -- stopping (left for watchdog).")
                break
            now = time.monotonic()

            # Benchmark daily-reset fix (22 Jul 2026): on a new UTC trading day, clear
            # each instrument's daily loss + kill switch so yesterday's loss does not
            # carry into today (CryptoBenchmark is 24/7 -> resets at 00:00 UTC).
            today_utc = datetime.now(timezone.utc).date()
            if today_utc != current_day:
                current_day = today_utc
                for inst in (btc, eth):
                    reset_daily_account(inst.account)
                log.info("New UTC trading day %s -- daily P&L + kill switch reset (BTC+ETH).", today_utc)

            # In-trade risk monitor (every 30s)
            if (now - last_monitor) >= MONITOR_SECONDS:
                for inst in (btc, eth):
                    if inst.strategy.in_trade:
                        try:
                            b5 = inst.feed.latest_bar("5m")
                            monitor_position(inst, _live_price(inst, b5))
                        except Exception as exc:
                            log.warning("[%s] monitor error: %s", inst.label, exc)
                last_monitor = now

            # Candle tick (every 5 min): decide + maybe enter
            if (now - last_candle) >= CANDLE_SECONDS:
                try:
                    btc.feed.refresh()
                    btc_atr = btc.feed.latest_bar("5m").get("atr")   # shared vol gate
                except Exception:
                    btc_atr = None
                for inst in (btc, eth):
                    try:
                        instrument_tick(inst, btc_atr)
                    except Exception as exc:
                        log.warning("[%s] tick error: %s", inst.label, exc)
                last_candle = now

            # Dashboard push (every ~15s)
            if (now - last_push) >= 15:
                push_dashboard(btc, eth, direction_switch.get_mode())
                last_push = now

            time.sleep(2)
        except Exception as exc:
            log.error("Main loop error: %s", exc)
            time.sleep(30)

    log.info("CryptoBenchmark stopped. Final capital: BTC £%.2f + ETH £%.2f = £%.2f",
             btc.strategy.capital_gbp, eth.strategy.capital_gbp,
             btc.strategy.capital_gbp + eth.strategy.capital_gbp)
    try:
        notifier.notify_system_startup  # noqa -- (startup only; no shutdown notifier on crypto)
    except Exception:
        pass


if __name__ == "__main__":
    main()
