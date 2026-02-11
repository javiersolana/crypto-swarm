"""
Paper Trader - Simulated trading with TP/SL/Trailing Stop (v8.1)

v7.0 Moonbag Strategy:
  - TP1 at +80%: sell 50% of position to secure capital
  - Trailing Stop 20% on remaining 50% (the "moonbag")
  - Emergency Exit: if Rugcheck reports "Danger", close immediately

v8.1 Fixes:
  - close_trade() now persists final_change_pct, fee_sol, pnl_net_sol
    explicitly as floats (was missing, causing report scripts to show 0%)

Automatically "buys" on every PRIORITY alert and manages exits via a
background thread. All state persisted to data/paper_trades.json.

Thread-safe: main thread calls open_trade(), exit-manager thread calls
check_exits() and close_trade(). All mutations protected by _lock.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

import config
from utils import get_logger, safe_float

log = get_logger("paper_trader")


class PaperTrader:
    """SOL-based paper trading with Moonbag exit strategy (v7.0).

    Moonbag Strategy:
      - TP1 at +80%: sell 50% → secure initial capital
      - Trailing Stop 20% on remaining 50% → ride the moon
      - Emergency Exit on Rugcheck "Danger" → protect from rugs
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._file = config.PAPER_TRADES_FILE
        # v7.5: Verify DATA_DIR exists and is writable
        data_dir = os.path.dirname(self._file)
        os.makedirs(data_dir, exist_ok=True)
        if not os.path.isdir(data_dir):
            log.error(f"CRITICAL: DATA_DIR does not exist: {data_dir}")
        elif not os.access(data_dir, os.W_OK):
            log.error(f"CRITICAL: DATA_DIR not writable: {data_dir}")
        else:
            log.info(f"Paper trader DATA_DIR verified: {data_dir}")

        self._data = self._load()
        self._trade_counter = self._data.get("total_trades", 0)
        # v7.1: Force-create portfolio file on disk if it doesn't exist
        if not os.path.exists(self._file):
            self._save()
            log.info(f"Paper trades file created: {self._file}")
        log.info(f"Paper trader ready: file={self._file}, "
                 f"open={len(self._data.get('open_trades', []))}, "
                 f"closed={len(self._data.get('closed_trades', []))}")

    def _load(self) -> dict:
        """Load state from JSON, or create fresh session."""
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            if os.path.exists(self._file):
                with open(self._file, "r") as f:
                    data = json.load(f)
                if data.get("open_trades") is not None:
                    return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Paper trades load error: {e}")

        return {
            "session_start": datetime.now(timezone.utc).isoformat(),
            "config": {
                "amount_sol": config.PAPER_TRADE_AMOUNT_SOL,
                "tp1_pct": config.PAPER_TP1_PCT,
                "tp1_sell_fraction": config.PAPER_TP1_SELL_FRACTION,
                "moonbag_trailing_pct": config.PAPER_MOONBAG_TRAILING_PCT,
                "sl_pct": config.PAPER_SL_PCT,
            },
            "open_trades": [],
            "closed_trades": [],
            "session_pnl_sol": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
        }

    def _save(self):
        """Persist to JSON. Must be called with _lock held.

        v7.5: Explicit ERROR log with full path on failure. Verifies
        file was actually written (size > 0) to catch silent corruption.
        """
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
            # Verify write succeeded
            if os.path.getsize(self._file) == 0:
                log.error(f"SAVE FAILED: {self._file} is empty after write!")
        except OSError as e:
            log.error(f"SAVE FAILED: Cannot write {self._file} — {e}")
        except (TypeError, ValueError) as e:
            log.error(f"SAVE FAILED: JSON serialization error — {e}")

    def open_trade(self, token: dict) -> dict | None:
        """Open a paper trade for an alerted token.

        Returns the trade dict, or None if duplicate/max reached.
        """
        address = token.get("address", "")
        if not address:
            return None

        with self._lock:
            # Check for duplicate open trade
            for t in self._data["open_trades"]:
                if t["address"].lower() == address.lower():
                    return None

            # Check max open trades
            if len(self._data["open_trades"]) >= config.PAPER_MAX_OPEN_TRADES:
                log.warning("Paper trader: max open trades reached")
                return None

            price = safe_float(token.get("price_usd"))
            if price <= 0:
                return None

            # v8.0: Apply buy slippage to simulate real execution
            price = price * (1 + config.PAPER_SLIPPAGE_PCT / 100)

            self._trade_counter += 1
            amount_sol = config.PAPER_TRADE_AMOUNT_SOL
            quantity = amount_sol / price if price > 0 else 0

            sl_price = price * (1 + config.PAPER_SL_PCT / 100)  # SL_PCT is negative
            tp1_price = price * (1 + config.PAPER_TP1_PCT / 100)

            trade = {
                "id": f"trade_{self._trade_counter:04d}",
                "token_name": token.get("name") or address[:8],
                "token_symbol": token.get("symbol") or (token.get("name") or address[:6])[:6],
                "address": address,
                "chain": token.get("chain", token.get("network", "")),
                "pool_address": token.get("pool_address", ""),
                "entry_price": price,
                "current_price": price,
                "amount_sol": amount_sol,
                "remaining_sol": amount_sol,  # v7.0: tracks remaining after TP1
                "quantity": quantity,
                "stop_loss": sl_price,
                "take_profit_1": tp1_price,
                "tp1_hit": False,             # v7.0: TP1 already triggered?
                "tp1_pnl_sol": 0.0,           # v7.0: PnL from TP1 partial close
                "trailing_activated": False,
                "trailing_pct": config.PAPER_MOONBAG_TRAILING_PCT,
                "highest_price": price,
                "status": "open",
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "alpha_score": safe_float(token.get("alpha_score")),
                "signal_count": token.get("signal_count", 0),
                "forense_score": safe_float(token.get("forense_score")),
            }

            self._data["open_trades"].append(trade)
            self._data["total_trades"] = self._trade_counter
            self._save()

        log.info(f"PAPER BUY: {trade['token_name']} @ ${price:.8f} "
                 f"(TP1=${tp1_price:.8f} [+{config.PAPER_TP1_PCT}%], "
                 f"SL=${sl_price:.8f} [{config.PAPER_SL_PCT}%])")
        return trade

    def _partial_close_tp1(self, trade: dict, current_price: float) -> dict | None:
        """v7.0: Execute TP1 — sell 50% of position, activate moonbag trailing.

        Returns a partial-close result dict for notification, or None.
        Must be called with _lock held.
        """
        if trade.get("tp1_hit"):
            return None

        sell_fraction = config.PAPER_TP1_SELL_FRACTION
        sold_sol = trade["remaining_sol"] * sell_fraction
        pnl_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
        pnl_sol = sold_sol * (pnl_pct / 100)

        trade["tp1_hit"] = True
        trade["tp1_pnl_sol"] = round(pnl_sol, 6)
        trade["remaining_sol"] = round(trade["remaining_sol"] - sold_sol, 6)
        trade["tp1_price"] = current_price
        trade["tp1_at"] = datetime.now(timezone.utc).isoformat()

        # Activate moonbag trailing stop: SL = current_price * (1 - trailing_pct/100)
        trade["trailing_activated"] = True
        trailing_sl = current_price * (1 - config.PAPER_MOONBAG_TRAILING_PCT / 100)
        trade["stop_loss"] = trailing_sl

        # Record realized PnL from TP1
        self._data["session_pnl_sol"] = round(
            self._data["session_pnl_sol"] + pnl_sol, 6
        )

        log.info(f"MOONBAG TP1: {trade['token_name']} — sold {sell_fraction*100:.0f}% "
                 f"@ ${current_price:.8f} (+{pnl_pct:.1f}%, +{pnl_sol:.4f} SOL). "
                 f"Trailing {config.PAPER_MOONBAG_TRAILING_PCT}% on remaining {trade['remaining_sol']:.4f} SOL")

        return {
            "trade_id": trade["id"],
            "token_name": trade["token_name"],
            "chain": trade["chain"],
            "type": "tp1_partial",
            "sold_fraction": sell_fraction,
            "exit_price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_sol": round(pnl_sol, 6),
            "remaining_sol": trade["remaining_sol"],
            "trailing_sl": trailing_sl,
        }

    def close_trade(self, trade_id: str, exit_price: float, reason: str) -> dict | None:
        """Close a trade (full or remaining moonbag), compute PnL, move to closed."""
        with self._lock:
            trade = None
            for i, t in enumerate(self._data["open_trades"]):
                if t["id"] == trade_id:
                    trade = self._data["open_trades"].pop(i)
                    break

            if not trade:
                return None

            # PnL on remaining position (after TP1 partial close, if any)
            remaining_sol = trade.get("remaining_sol", trade["amount_sol"])
            pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"]) * 100
            pnl_sol_remaining = remaining_sol * (pnl_pct / 100)
            tp1_pnl = trade.get("tp1_pnl_sol", 0)
            # v8.0: Deduct simulated fees (Priority Fees + Jito Tips)
            fee_sol = config.PAPER_FEE_SOL
            total_pnl_sol = pnl_sol_remaining + tp1_pnl - fee_sol
            result = "WIN" if total_pnl_sol > 0 else "LOSS"

            trade.update({
                "exit_price": exit_price,
                "current_price": exit_price,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
                "pnl_sol": round(total_pnl_sol, 6),
                "pnl_pct": round(pnl_pct, 2),
                "final_change_pct": float(round(pnl_pct, 2)),  # v8.1: explicit float for reports
                "fee_sol": float(round(fee_sol, 6)),            # v8.1: persisted fee
                "pnl_net_sol": float(round(total_pnl_sol, 6)),  # v8.1: net PnL after fees
                "pnl_sol_remaining": round(pnl_sol_remaining, 6),
                "exit_reason": reason,
                "status": "closed",
            })

            self._data["closed_trades"].append(trade)
            # Only add remaining position PnL (TP1 was already added)
            self._data["session_pnl_sol"] = round(
                self._data["session_pnl_sol"] + pnl_sol_remaining, 6
            )
            if result == "WIN":
                self._data["wins"] += 1
            else:
                self._data["losses"] += 1

            self._save()

        log.info(f"PAPER {reason.upper()}: {trade['token_name']} "
                 f"PnL={pnl_pct:+.1f}% (remaining: {pnl_sol_remaining:+.4f} SOL, "
                 f"TP1: {tp1_pnl:+.4f} SOL, fees: -{fee_sol} SOL, "
                 f"net: {total_pnl_sol:+.4f} SOL)")
        return trade

    def get_open_trades(self) -> list:
        """Return a copy of open trades (thread-safe)."""
        with self._lock:
            return list(self._data["open_trades"])

    def update_prices(self, price_map: dict):
        """Update current_price and highest_price. Manages moonbag trailing SL.

        v7.0: After TP1, trailing stop follows the highest_price with a 20% buffer.
        """
        with self._lock:
            changed = False
            for trade in self._data["open_trades"]:
                addr = trade["address"].lower()
                current = price_map.get(addr)
                if current is None or current <= 0:
                    continue

                trade["current_price"] = current
                if current > trade["highest_price"]:
                    trade["highest_price"] = current

                # v7.0: If trailing is active (post-TP1), ratchet the SL up
                if trade.get("trailing_activated") and trade.get("tp1_hit"):
                    trailing_pct = trade.get("trailing_pct", config.PAPER_MOONBAG_TRAILING_PCT)
                    new_sl = trade["highest_price"] * (1 - trailing_pct / 100)
                    if new_sl > trade["stop_loss"]:
                        trade["stop_loss"] = new_sl

                changed = True

            if changed:
                self._save()

    def check_exits(self, price_map: dict) -> list[dict]:
        """Check TP1/SL/Trailing for all open trades.

        v7.0 Moonbag flow:
          1. Check TP1 (+80%): partial sell 50%
          2. Check trailing stop on moonbag (20% from highest)
          3. Check initial stop loss (-25%)

        Returns list of closed trade dicts.
        """
        exits = []
        tp1_results = []

        with self._lock:
            for trade in list(self._data["open_trades"]):
                addr = trade["address"].lower()
                current = price_map.get(addr)
                if current is None or current <= 0:
                    continue

                pnl_pct = ((current - trade["entry_price"]) / trade["entry_price"]) * 100

                # v7.0: Check TP1 (partial close at +80%)
                if not trade.get("tp1_hit") and pnl_pct >= config.PAPER_TP1_PCT:
                    result = self._partial_close_tp1(trade, current)
                    if result:
                        tp1_results.append(result)
                    continue  # Don't close fully — moonbag continues

                # Check trailing stop (moonbag or legacy)
                if current <= trade["stop_loss"]:
                    if trade.get("tp1_hit"):
                        reason = "moonbag_trailing"
                    elif trade.get("trailing_activated"):
                        reason = "trailing_stop"
                    else:
                        reason = "stop_loss"
                    exits.append({"trade_id": trade["id"], "price": current, "reason": reason})

            if tp1_results:
                self._save()

        # Execute full exits outside the main lock
        closed = []
        for exit_info in exits:
            result = self.close_trade(exit_info["trade_id"], exit_info["price"], exit_info["reason"])
            if result:
                closed.append(result)

        # Attach TP1 partial results for notification
        for tp1 in tp1_results:
            tp1["_is_partial"] = True
            closed.append(tp1)

        return closed

    def emergency_exit(self, address: str, current_price: float) -> dict | None:
        """v7.0: Emergency close on Rugcheck Danger status."""
        with self._lock:
            for trade in self._data["open_trades"]:
                if trade["address"].lower() == address.lower():
                    trade_id = trade["id"]
                    break
            else:
                return None

        result = self.close_trade(trade_id, current_price, "emergency_rugcheck")
        if result:
            log.warning(f"EMERGENCY EXIT: {result['token_name']} — Rugcheck Danger detected")
        return result

    def get_session_summary(self) -> dict:
        """Return session PnL summary."""
        with self._lock:
            total = self._data["wins"] + self._data["losses"]
            win_rate = (self._data["wins"] / total * 100) if total > 0 else 0
            return {
                "session_pnl_sol": self._data["session_pnl_sol"],
                "total_trades": self._data.get("total_trades", 0),
                "open_trades": len(self._data["open_trades"]),
                "closed_trades": len(self._data["closed_trades"]),
                "wins": self._data["wins"],
                "losses": self._data["losses"],
                "win_rate": round(win_rate, 1),
            }

    def format_exit_message(self, trade: dict) -> str:
        """Format a Telegram HTML message for a trade exit or TP1 partial."""
        summary = self.get_session_summary()

        # v7.0: Handle TP1 partial close notification
        if trade.get("_is_partial"):
            return (
                f"<b>MOONBAG TP1</b>\n\n"
                f"<b>{trade['token_name']}</b> ({trade['chain']})\n"
                f"Sold {trade['sold_fraction']*100:.0f}% @ ${trade['exit_price']:.8f}\n"
                f"PnL: <b>+{trade['pnl_pct']:.1f}%</b> ({trade['pnl_sol']:+.4f} SOL)\n"
                f"Remaining: {trade['remaining_sol']:.4f} SOL (moonbag)\n"
                f"Trailing SL: ${trade['trailing_sl']:.8f} "
                f"(-{config.PAPER_MOONBAG_TRAILING_PCT}%)\n\n"
                f"--- Session ---\n"
                f"Total PnL: {summary['session_pnl_sol']:+.4f} SOL"
            )

        pnl_pct = trade.get("pnl_pct", 0)
        pnl_sol = trade.get("pnl_sol", 0)
        tp1_pnl = trade.get("tp1_pnl_sol", 0)
        remaining_pnl = trade.get("pnl_sol_remaining", pnl_sol)

        reason_display = trade.get("exit_reason", "unknown").replace("_", " ").title()
        if trade.get("exit_reason") == "emergency_rugcheck":
            reason_display = "EMERGENCY (Rugcheck Danger)"

        tp1_line = ""
        if trade.get("tp1_hit"):
            tp1_line = f"TP1 realized: {tp1_pnl:+.4f} SOL\n"

        return (
            f"<b>PAPER {'EXIT' if pnl_sol > 0 else 'STOPPED'}</b>\n\n"
            f"<b>{trade['token_name']}</b> ({trade['chain']})\n"
            f"Reason: <b>{reason_display}</b>\n\n"
            f"Entry: ${trade['entry_price']:.8f}\n"
            f"Exit: ${trade.get('exit_price', 0):.8f}\n"
            f"{tp1_line}"
            f"Moonbag PnL: {remaining_pnl:+.4f} SOL\n"
            f"Total PnL: <b>{pnl_sol:+.4f} SOL</b> ({pnl_pct:+.1f}%)\n"
            f"Highest: ${trade['highest_price']:.8f}\n\n"
            f"--- Session ---\n"
            f"Total PnL: {summary['session_pnl_sol']:+.4f} SOL\n"
            f"W/L: {summary['wins']}/{summary['losses']} "
            f"({summary['win_rate']:.0f}% WR)\n"
            f"Open: {summary['open_trades']}"
        )

    def format_open_message(self, trade: dict) -> str:
        """Format a Telegram HTML message for a new paper trade."""
        summary = self.get_session_summary()
        return (
            f"<b>PAPER BUY</b>\n\n"
            f"<b>{trade['token_name']}</b> ({trade['chain']})\n"
            f"Price: ${trade['entry_price']:.8f}\n"
            f"Amount: {trade['amount_sol']} SOL\n"
            f"TP1: ${trade['take_profit_1']:.8f} (+{config.PAPER_TP1_PCT}%, sell 50%)\n"
            f"SL: ${trade['stop_loss']:.8f} ({config.PAPER_SL_PCT}%)\n"
            f"Moonbag trailing: {config.PAPER_MOONBAG_TRAILING_PCT}% after TP1\n"
            f"Score: forense={trade['forense_score']:.1f}\n\n"
            f"Open trades: {summary['open_trades']}"
        )
