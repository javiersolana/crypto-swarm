# CRYPTO SWARM UPGRADE: Auto-Discovery + Backtesting + API Fix

## ROLE
You are a senior Python developer fixing critical performance issues and adding automation to a crypto tracking system.

## TASK
Fix 3 critical problems in existing crypto-swarm system:
1. **URGENT**: Helius API timeouts (15min/scan → target: 2min/scan)
2. Auto-discover profitable wallets daily
3. Validate strategy with backtesting

## CONTEXT
Current state:
- System: ~/crypto-swarm with swarm_v2.py, alpha_monitor.py
- Problem: `alpha_monitor.py --wallets-only` takes 15min per scan (Helius timeouts)
- Log shows: `ReadTimeoutError` on every wallet request
- 22 wallets tracked, most are fake/inactive

## SUCCESS CRITERIA
✅ Wallet scanning completes in <3 minutes (not 15min)
✅ Auto-discovery finds 20+ profitable wallets daily from gmgn.ai
✅ Backtester shows historical win rate of past alerts
✅ All fixes integrate seamlessly (don't break existing code)

## CONSTRAINTS
- Keep existing file structure
- Only use FREE APIs
- Code must be production-ready (error handling, logging)
- No breaking changes to swarm_v2.py or alert_monitor.py

---

## IMPLEMENTATION

### PRIORITY 1: Fix Helius Timeout (URGENT)

**File:** `alpha/smart_wallet_tracker.py` (modify existing)

**Problem:**
```python
# Current: Every wallet call times out in 10s
response = requests.get(helius_url, timeout=10)
```

**Solution Options (implement best one):**
A. Switch to Solana RPC public endpoint (free, faster)
B. Implement aggressive caching (5min cache per wallet)
C. Parallel requests (ThreadPoolExecutor)
D. Combination of B+C

**Requirements:**
- Reduce scan time: 15min → <3min
- Fallback chain: Helius → Solana RPC → skip wallet
- Add metric logging: `[wallet_tracker] INFO: Scan completed in 2m 34s (22 wallets)`

---

### PRIORITY 2: Auto-Discovery

**Create:** `wallet_discovery.py`
```python
"""
Auto-discover profitable Solana wallets from gmgn.ai
Run daily via cron to refresh tracked wallets
"""

import requests
from bs4 import BeautifulSoup
import json

def scrape_gmgn_top_traders():
    """
    Scrape https://gmgn.ai/monitor/Leveling?chain=sol
    Extract: wallet address, PNL_30d, win_rate, trades_count
    Filter: PNL >$10k, win_rate >55%, trades >20
    Return: list of top 50 wallets
    """
    pass

def filter_wallets(raw_wallets):
    """Apply filters, deduplicate, sort by PNL"""
    pass

def update_tracked_wallets(new_wallets):
    """
    Replace old wallets in .wallet_tracker/tracked_wallets.json
    Keep wallets with recent activity (<7 days)
    """
    pass

if __name__ == "__main__":
    # CLI: python3 wallet_discovery.py --refresh
    pass
```

**Requirements:**
- Output: `wallet_discovery_report.json` with found wallets
- Integration: Auto-add to smart_wallet_tracker.py
- Logging: Show how many new wallets added/removed

---

### PRIORITY 3: Backtesting

**Create:** `backtester.py`
```python
"""
Backtest historical alerts to measure strategy performance
"""

def load_historical_alerts():
    """Read data/alerts.json"""
    pass

def fetch_current_prices(tokens):
    """Get current price for each alerted token"""
    pass

def calculate_performance(alerts):
    """
    For each alert:
    - entry_price (from alert)
    - current_price (from API)
    - gain% = (current - entry) / entry * 100
    - would_profit = gain% > 0
    
    Output metrics:
    - win_rate = profitable_alerts / total_alerts
    - avg_gain% = mean(gain%)
    - best_trade, worst_trade
    """
    pass

if __name__ == "__main__":
    # CLI: python3 backtester.py --last-7-days
    pass
```

**Requirements:**
- Output: Markdown table + JSON report
- Handle missing data gracefully (token no longer exists)
- Compare: if bought at alert price, what would P&L be now?

---

## VERIFICATION CHECKLIST

After implementation, run:
```bash
# 1. Test fixed wallet tracker
time python3 alpha/smart_wallet_tracker.py --scan-once
# Expected: <3 minutes, no timeouts

# 2. Test discovery
python3 wallet_discovery.py --test
# Expected: finds 20+ wallets from gmgn.ai

# 3. Test backtester
python3 backtester.py --last-7-days
# Expected: shows win_rate, avg_gain

# 4. Integration test
pkill -f alpha_monitor
nohup python3 alpha_monitor.py --wallets-only > test.log 2>&1 &
sleep 180  # 3 minutes
tail -50 test.log
# Expected: "Scan completed in <3min"
```

---

## OUTPUT FORMAT

For each file modified/created:

1. **Show full file path**
2. **Show complete code** (no truncation)
3. **Explain key changes** (1-2 sentences)
4. **Provide test command**

Example:
```
FILE: alpha/smart_wallet_tracker.py
CHANGES: Added ThreadPoolExecutor for parallel requests, 5min cache
TEST: time python3 alpha/smart_wallet_tracker.py --scan-once
```

---

## DEBUGGING HINTS

If still getting timeouts:
- Check: Is Helius free tier rate-limited? (250k req/day)
- Solution: Switch to https://api.mainnet-beta.solana.com (free, no limit)

If gmgn.ai scraping fails:
- Use Selenium headless if site requires JS
- Fallback: Manual wallet list from Dune Analytics

---

## CRITICAL NOTES

- **Don't break existing code**: Test everything before delivery
- **Prioritize speed**: Fix timeout issue FIRST, other features second
- **Production quality**: Add try/except, logging, graceful degradation

START NOW. Fix timeout issue first, then implement discovery + backtesting.
