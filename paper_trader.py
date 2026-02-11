"""
Paper Trader - Simulated trading with TP/SL/Trailing Stop (v6.0)

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
    """SOL-based paper trading with trailing stop management."""

    def __init__(self):
        self._lock = threading.Lock()
        self._file = config.PAPER_TRADES_FILE
        self._data = self._load()
        self._trade_counter = self._data.get("total_trades", 0)

    def _load(self) -> dict:
        """Load state from JSON, or create fresh session."""
        try:
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
                "tp_pct": config.PAPER_TP_PCT,
                "sl_pct": config.PAPER_SL_PCT,
                "trailing_activation_pct": config.PAPER_TRAILING_ACTIVATION_PCT,
            },
            "open_trades": [],
            "closed_trades": [],
            "session_pnl_sol": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
        }

    def _save(self):
        """Persist to JSON. Must be called with _lock held."""
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
        except OSError as e:
            log.error(f"Paper trades save error: {e}")

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

            self._trade_counter += 1
            amount_sol = config.PAPER_TRADE_AMOUNT_SOL
            # Calculate quantity (tokens bought with amount_sol worth of USD)
            # For paper trading we track in USD terms via price
            quantity = amount_sol / price if price > 0 else 0

            sl_price = price * (1 + config.PAPER_SL_PCT / 100)  # SL_PCT is negative
            tp_price = price * (1 + config.PAPER_TP_PCT / 100)

            trade = {
                "id": f"trade_{self._trade_counter:04d}",
                "token_name": token.get("name", "?"),
                "token_symbol": token.get("symbol", token.get("name", "?")[:6]),
                "address": address,
                "chain": token.get("chain", token.get("network", "")),
                "pool_address": token.get("pool_address", ""),
                "entry_price": price,
                "current_price": price,
                "amount_sol": amount_sol,
                "quantity": quantity,
                "stop_loss": sl_price,
                "take_profit": tp_price,
                "trailing_activated": False,
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
                 f"(TP=${tp_price:.8f}, SL=${sl_price:.8f})")
        return trade

    def close_trade(self, trade_id: str, exit_price: float, reason: str) -> dict | None:
        """Close a trade, compute PnL, move to closed_trades."""
        with self._lock:
            trade = None
            for i, t in enumerate(self._data["open_trades"]):
                if t["id"] == trade_id:
                    trade = self._data["open_trades"].pop(i)
                    break

            if not trade:
                return None

            pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"]) * 100
            pnl_sol = trade["amount_sol"] * (pnl_pct / 100)
            result = "WIN" if pnl_pct > 0 else "LOSS"

            trade.update({
                "exit_price": exit_price,
                "current_price": exit_price,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
                "pnl_sol": round(pnl_sol, 6),
                "pnl_pct": round(pnl_pct, 2),
                "exit_reason": reason,
                "status": "closed",
            })

            self._data["closed_trades"].append(trade)
            self._data["session_pnl_sol"] = round(
                self._data["session_pnl_sol"] + pnl_sol, 6
            )
            if result == "WIN":
                self._data["wins"] += 1
            else:
                self._data["losses"] += 1

            self._save()

        log.info(f"PAPER {reason.upper()}: {trade['token_name']} "
                 f"PnL={pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)")
        return trade

    def get_open_trades(self) -> list:
        """Return a copy of open trades (thread-safe)."""
        with self._lock:
            return list(self._data["open_trades"])

    def update_prices(self, price_map: dict):
        """Update current_price and highest_price for all open trades.

        price_map: {address_lower: price_usd}
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

                # Check trailing stop activation
                pnl_pct = ((current - trade["entry_price"]) / trade["entry_price"]) * 100
                if not trade["trailing_activated"] and pnl_pct >= config.PAPER_TRAILING_ACTIVATION_PCT:
                    trade["trailing_activated"] = True
                    trade["stop_loss"] = trade["entry_price"]  # move SL to break-even
                    log.info(f"TRAILING ACTIVATED: {trade['token_name']} "
                             f"at +{pnl_pct:.0f}% (SL -> break-even)")

                changed = True

            if changed:
                self._save()

    def check_exits(self, price_map: dict) -> list[dict]:
        """Check TP/SL/Trailing for all open trades. Returns list of exit info dicts."""
        exits = []
        with self._lock:
            for trade in list(self._data["open_trades"]):
                addr = trade["address"].lower()
                current = price_map.get(addr)
                if current is None or current <= 0:
                    continue

                pnl_pct = ((current - trade["entry_price"]) / trade["entry_price"]) * 100

                # Check Take Profit
                if pnl_pct >= config.PAPER_TP_PCT:
                    exits.append({"trade_id": trade["id"], "price": current, "reason": "take_profit"})
                    continue

                # Check Stop Loss (or trailing stop at break-even)
                if current <= trade["stop_loss"]:
                    reason = "trailing_stop" if trade["trailing_activated"] else "stop_loss"
                    exits.append({"trade_id": trade["id"], "price": current, "reason": reason})

        # Execute exits outside the main lock iteration
        closed = []
        for exit_info in exits:
            result = self.close_trade(exit_info["trade_id"], exit_info["price"], exit_info["reason"])
            if result:
                closed.append(result)

        return closed

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
        """Format a Telegram HTML message for a trade exit."""
        summary = self.get_session_summary()
        emoji = "+" if trade["pnl_pct"] > 0 else ""

        return (
            f"<b>PAPER TRADE {'EXIT' if trade['pnl_pct'] > 0 else 'STOPPED'}</b>\n\n"
            f"<b>{trade['token_name']}</b> ({trade['chain']})\n"
            f"Reason: <b>{trade['exit_reason'].replace('_', ' ').title()}</b>\n\n"
            f"Entry: ${trade['entry_price']:.8f}\n"
            f"Exit: ${trade['exit_price']:.8f}\n"
            f"PnL: <b>{emoji}{trade['pnl_pct']:.1f}%</b> ({trade['pnl_sol']:+.4f} SOL)\n"
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
            f"TP: ${trade['take_profit']:.8f} (+{config.PAPER_TP_PCT}%)\n"
            f"SL: ${trade['stop_loss']:.8f} ({config.PAPER_SL_PCT}%)\n"
            f"Score: forense={trade['forense_score']:.1f}\n\n"
            f"Open trades: {summary['open_trades']}"
        )
