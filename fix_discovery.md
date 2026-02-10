# FIX: wallet_discovery.py - gmgn.ai Scraping

## PROBLEM
Current code returns 0 wallets from gmgn.ai (requires JS rendering or cookies).

## TASK
Fix `wallet_discovery.py` to actually get wallet addresses using ONE of these methods:

### OPTION A: Use web_search + manual extraction
```python
# Use Claude's web_search tool to find recent articles mentioning wallet addresses
# Extract addresses from search results
```

### OPTION B: Scrape Dune Analytics
```python
# Alternative to gmgn.ai
# URL: https://dune.com/queries/...
# Easier to scrape, no JS needed
```

### OPTION C: Use gmgn.ai public leaderboard
```python
# Try direct HTML scraping of:
# https://gmgn.ai/board/sol/leaderboard
# With User-Agent header
```

## SUCCESS CRITERIA
- `python3 wallet_discovery.py --test` finds 20+ addresses
- Addresses are valid Solana base58 strings
- Can be integrated to smart_wallet_tracker.py

## CONSTRAINTS
- No Selenium (too heavy)
- Use requests + BeautifulSoup or web_search tool
- Must work headless (cron compatible)

Implement the EASIEST option that works. Test it.
