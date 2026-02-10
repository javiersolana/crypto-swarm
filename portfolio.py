"""
THE EXECUTOR - Portfolio Management & Capital Allocation
Computes composite scores, allocates capital, manages paper trading positions.
"""
from datetime import datetime, timezone

import config
from utils import get_logger, load_json, save_json, clamp, now_utc, format_usd

log = get_logger("executor")


class Executor:
    """Portfolio management with paper trading support."""

    def __init__(self, capital: float = None, mode: str = "paper"):
        self.total_capital = capital or config.DEFAULT_CAPITAL
        self.reserve = config.RESERVE_AMOUNT
        self.investable = self.total_capital - self.reserve
        self.mode = mode
        self.portfolio = self._load_portfolio()

    def _load_portfolio(self) -> dict:
        data = load_json(config.PORTFOLIO_FILE)
        if not data:
            data = {
                "mode": self.mode,
                "total_capital": self.total_capital,
                "reserve": self.reserve,
                "investable": self.investable,
                "positions": [],
                "closed_positions": [],
                "history": [],
                "created_at": now_utc().isoformat(),
                "updated_at": now_utc().isoformat(),
            }
        return data

    def _save_portfolio(self):
        self.portfolio["updated_at"] = now_utc().isoformat()
        save_json(config.PORTFOLIO_FILE, self.portfolio)

    def select_and_allocate(self, candidates: list[dict]) -> list[dict]:
        """Score, rank, and allocate capital to top candidates."""
        log.info(f"=== THE EXECUTOR: Selecting from {len(candidates)} candidates ===")

        # Compute composite scores
        scored = self._compute_composite_scores(candidates)
        scored.sort(key=lambda x: x["composite_score"], reverse=True)

        # Select top N
        num_positions = min(config.MAX_POSITIONS, max(config.MIN_POSITIONS, len(scored)))
        selected = scored[:num_positions]

        if not selected:
            log.warning("No candidates passed scoring. No positions to open.")
            return []

        # Allocate capital
        allocated = self._allocate_capital(selected)

        # Create paper positions
        positions = self._create_positions(allocated)

        # Save to portfolio
        self.portfolio["positions"] = positions
        self.portfolio["investable"] = self.investable
        self.portfolio["mode"] = self.mode
        self.portfolio["total_capital"] = self.total_capital

        # Add to history
        self.portfolio["history"].append({
            "timestamp": now_utc().isoformat(),
            "action": "weekly_rebalance",
            "num_positions": len(positions),
            "total_allocated": sum(p["allocated_eur"] for p in positions),
        })

        self._save_portfolio()

        log.info(f"=== THE EXECUTOR: {len(positions)} positions created ===")
        return positions

    def _compute_composite_scores(self, candidates: list[dict]) -> list[dict]:
        """Compute weighted composite score for each candidate."""
        for c in candidates:
            scout = c.get("scout_score", 5.0)
            forense = c.get("forense_score", 5.0)
            narrator = c.get("narrator_score", 5.0)
            quant = c.get("quant_score", 5.0)

            # Executor score: based on portfolio fit
            executor = self._compute_executor_score(c)
            c["executor_score"] = executor

            composite = (
                config.WEIGHTS["scout"] * scout +
                config.WEIGHTS["forense"] * forense +
                config.WEIGHTS["narrator"] * narrator +
                config.WEIGHTS["quant"] * quant +
                config.WEIGHTS["executor"] * executor
            )

            c["composite_score"] = round(clamp(composite, 0, 10), 2)
            log.info(
                f"  {c['name']}: composite={c['composite_score']:.2f} "
                f"(scout={scout:.1f}, forense={forense:.1f}, "
                f"narrator={narrator:.1f}, quant={quant:.1f}, executor={executor:.1f})"
            )

        return candidates

    def _compute_executor_score(self, token: dict) -> float:
        """Score based on portfolio fit and risk management."""
        score = 5.0

        # Prefer tokens not at ATH
        if token.get("at_ath"):
            score -= 3.0

        # Prefer tokens with accumulation
        if token.get("accumulation_detected"):
            score += 2.0

        # Prefer tokens with good discourse quality
        discourse = token.get("discourse_quality", "neutral")
        if discourse == "technical":
            score += 1.0
        elif discourse == "hype":
            score -= 0.5

        # Prefer tokens with clear entry below current price
        entry = token.get("entry_price", 0)
        current = token.get("price_usd", 0)
        if entry > 0 and current > 0 and entry < current:
            score += 1.0  # Good: entry is below current (limit order)

        # Prefer moderate forense score (not barely passing)
        forense = token.get("forense_score", 0)
        if forense >= 9:
            score += 1.0
        elif forense >= 8:
            score += 0.5

        # v2.0: Early entry bonus/penalty
        early_signals = token.get("early_entry_signals", [])
        if any("pre_pump_entry" in s for s in early_signals):
            score += 1.5  # Ideal: found before pump
        if any("late_entry" in s for s in early_signals):
            score -= 2.0  # Penalize: arrived after pump
        if any("coordinated_pump" in s for s in early_signals):
            score -= 3.0  # Strong penalty

        # v2.0: Volume divergence penalty
        quant_signals = token.get("quant_signals", [])
        if any("BEARISH_DIVERGENCE" in s for s in quant_signals):
            score -= 2.0  # Price rising on declining volume = dump incoming

        # Diversification: check existing positions for network overlap
        existing_networks = {p["network"] for p in self.portfolio.get("positions", [])}
        if token.get("network") not in existing_networks:
            score += 0.5

        return clamp(score, 1, 10)

    def _allocate_capital(self, selected: list[dict]) -> list[dict]:
        """Allocate EUR capital to selected tokens."""
        if not selected:
            return []

        max_per_position = self.investable * (config.MAX_POSITION_PCT / 100)

        # Score-weighted allocation
        total_score = sum(t["composite_score"] for t in selected)
        if total_score == 0:
            total_score = len(selected)

        for token in selected:
            weight = token["composite_score"] / total_score
            raw_allocation = self.investable * weight

            # Cap at max per position
            allocation = min(raw_allocation, max_per_position)
            token["allocated_eur"] = round(allocation, 2)

        # Normalize if total exceeds investable
        total_allocated = sum(t["allocated_eur"] for t in selected)
        if total_allocated > self.investable:
            factor = self.investable / total_allocated
            for token in selected:
                token["allocated_eur"] = round(token["allocated_eur"] * factor, 2)

        return selected

    def _create_positions(self, allocated: list[dict]) -> list[dict]:
        """Create paper trading positions."""
        positions = []
        for token in allocated:
            entry_price = token.get("entry_price", 0) or token.get("price_usd", 0)
            if entry_price <= 0:
                continue

            allocated_eur = token["allocated_eur"]
            # Approximate EUR to USD (rough estimate)
            allocated_usd = allocated_eur * 1.08

            quantity = allocated_usd / entry_price if entry_price > 0 else 0
            stop_loss = entry_price * (1 + config.STOP_LOSS_PCT / 100)
            take_profit = entry_price * (1 + config.TAKE_PROFIT_PCT / 100)

            position = {
                "name": token.get("name", ""),
                "symbol": token.get("symbol", token.get("name", "")[:6].upper()),
                "address": token.get("address", ""),
                "pool_address": token.get("pool_address", ""),
                "network": token.get("network", ""),
                "chain": token.get("chain", ""),
                "entry_price": entry_price,
                "current_price": token.get("price_usd", entry_price),
                "quantity": quantity,
                "allocated_eur": allocated_eur,
                "allocated_usd": allocated_usd,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "composite_score": token.get("composite_score", 0),
                "scout_score": token.get("scout_score", 0),
                "forense_score": token.get("forense_score", 0),
                "narrator_score": token.get("narrator_score", 0),
                "quant_score": token.get("quant_score", 0),
                "executor_score": token.get("executor_score", 0),
                "signals": {
                    "forense_flags": token.get("forense_flags", []),
                    "narrator_signals": token.get("narrator_signals", []),
                    "quant_signals": token.get("quant_signals", []),
                    "early_entry_signals": token.get("early_entry_signals", []),
                },
                "status": "open",
                "opened_at": now_utc().isoformat(),
                "pnl_pct": 0.0,
                "pnl_eur": 0.0,
            }

            positions.append(position)
            log.info(
                f"  Position: {position['name']} | "
                f"{format_usd(allocated_usd)} | "
                f"Entry: ${entry_price:.6f} | "
                f"SL: ${stop_loss:.6f} | "
                f"TP: ${take_profit:.6f}"
            )

        return positions

    def update_positions(self) -> list[dict]:
        """Update current positions with latest prices (for tracking)."""
        from api_client import DexScreenerClient
        dex = DexScreenerClient()

        for pos in self.portfolio.get("positions", []):
            if pos["status"] != "open":
                continue

            if pos.get("address") and pos.get("chain"):
                pairs = dex.get_token_pairs(pos["chain"], pos["address"])
                if pairs:
                    new_price = float(pairs[0].get("priceUsd", 0))
                    if new_price > 0:
                        pos["current_price"] = new_price
                        entry = pos["entry_price"]
                        pos["pnl_pct"] = ((new_price - entry) / entry) * 100
                        pos["pnl_eur"] = pos["allocated_eur"] * (pos["pnl_pct"] / 100)

                        # Check stop-loss
                        if new_price <= pos["stop_loss"]:
                            pos["status"] = "stopped_out"
                            log.warning(f"STOP-LOSS triggered: {pos['name']}")

                        # Check take-profit
                        if new_price >= pos["take_profit"]:
                            pos["status"] = "take_profit_hit"
                            log.info(f"TAKE-PROFIT hit: {pos['name']}")

        self._save_portfolio()
        return self.portfolio.get("positions", [])

    def get_portfolio_summary(self) -> dict:
        """Get current portfolio summary."""
        positions = self.portfolio.get("positions", [])
        open_positions = [p for p in positions if p["status"] == "open"]

        total_allocated = sum(p["allocated_eur"] for p in open_positions)
        total_pnl = sum(p.get("pnl_eur", 0) for p in open_positions)

        return {
            "mode": self.mode,
            "total_capital": self.total_capital,
            "reserve": self.reserve,
            "investable": self.investable,
            "allocated": total_allocated,
            "available": self.investable - total_allocated,
            "num_positions": len(open_positions),
            "total_pnl_eur": total_pnl,
            "positions": open_positions,
        }
