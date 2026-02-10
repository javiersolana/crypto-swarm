#!/usr/bin/env python3
"""
Social Intelligence Module - Detect narratives before they explode.

Combines multiple free data sources:
1. CryptoPanic API - Trending crypto news with sentiment
2. GitHub API - Repository activity for token projects
3. DexScreener profiles - Social presence detection
4. Jupiter token list - New Solana token detection

Usage:
  python3 alpha/social_intel.py --scan             # scan for social signals
  python3 alpha/social_intel.py --github <token>   # check GitHub activity
  python3 alpha/social_intel.py --news             # get trending crypto news
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import config_alpha
from utils import get_logger, setup_logging, load_json, save_json, now_utc, safe_float

log = get_logger("social_intel")


# ─── CryptoPanic Client (News + Sentiment) ───────────────────────────────

class CryptoPanicClient:
    """CryptoPanic API for crypto news aggregation and sentiment."""

    def __init__(self):
        self.api_key = config_alpha.CRYPTOPANIC_API_KEY
        self.base_url = config_alpha.CRYPTOPANIC_BASE
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < config_alpha.CRYPTOPANIC_DELAY:
            time.sleep(config_alpha.CRYPTOPANIC_DELAY - elapsed)
        self._last_request = time.time()

    def _get(self, endpoint: str, params: dict = None) -> dict | None:
        self._rate_limit()
        params = params or {}
        if self.api_key:
            params["auth_token"] = self.api_key

        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}/{endpoint}?{query}"

        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.warning(f"CryptoPanic request failed: {e}")
            return None

    def get_trending_news(self, filter_type: str = "hot") -> list[dict]:
        """
        Get trending crypto news.
        filter_type: 'hot' (trending), 'rising', 'bullish', 'bearish', 'important'
        """
        data = self._get("posts/", params={
            "filter": filter_type,
            "kind": "news",
            "regions": "en",
        })
        if not data or "results" not in data:
            return []
        return data["results"]

    def search_token_news(self, token_symbol: str) -> list[dict]:
        """Search for news mentioning a specific token."""
        data = self._get("posts/", params={
            "currencies": token_symbol.upper(),
            "kind": "news",
            "regions": "en",
        })
        if not data or "results" not in data:
            return []
        return data["results"]

    def get_news_sentiment(self, news_items: list[dict]) -> dict:
        """Analyze sentiment from a list of news items."""
        if not news_items:
            return {"score": 5.0, "total": 0, "positive": 0, "negative": 0}

        positive = 0
        negative = 0
        neutral = 0

        for item in news_items:
            votes = item.get("votes", {})
            pos = votes.get("positive", 0)
            neg = votes.get("negative", 0)

            if pos > neg:
                positive += 1
            elif neg > pos:
                negative += 1
            else:
                neutral += 1

            # Also check if CryptoPanic has classified it
            kind = item.get("kind", "")
            if kind == "bullish":
                positive += 1
            elif kind == "bearish":
                negative += 1

        total = len(news_items)
        if total == 0:
            return {"score": 5.0, "total": 0, "positive": 0, "negative": 0}

        # Score: 0-10 scale
        pos_ratio = positive / total
        score = 5.0 + (pos_ratio - 0.5) * 8  # Range: 1.0 - 9.0
        score = max(1.0, min(9.0, score))

        return {
            "score": round(score, 1),
            "total": total,
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
        }

    def extract_mentioned_tokens(self, news_items: list[dict]) -> dict:
        """Extract token mentions from news, return {symbol: mention_count}."""
        mentions = {}
        for item in news_items:
            currencies = item.get("currencies", [])
            for currency in currencies:
                code = currency.get("code", "").upper()
                if code and code not in ("BTC", "ETH", "SOL", "USDT", "USDC"):
                    mentions[code] = mentions.get(code, 0) + 1
        return dict(sorted(mentions.items(), key=lambda x: x[1], reverse=True))


# ─── GitHub Activity Monitor ─────────────────────────────────────────────

class GitHubMonitor:
    """Monitor GitHub activity for crypto projects."""

    def __init__(self):
        self.token = config_alpha.GITHUB_TOKEN
        self.base_url = config_alpha.GITHUB_API
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < config_alpha.GITHUB_DELAY:
            time.sleep(config_alpha.GITHUB_DELAY - elapsed)
        self._last_request = time.time()

    def _get(self, endpoint: str, params: dict = None) -> dict | list | None:
        self._rate_limit()
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if query:
            url += f"?{query}"

        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.warning(f"GitHub API failed: {e}")
            return None

    def search_repos(self, token_name: str, token_symbol: str = "") -> list[dict]:
        """Search GitHub for repositories related to a token."""
        queries = []
        # Search by name
        if token_name:
            clean_name = token_name.strip().lower()
            # Skip generic names that would return noise
            skip_names = {"token", "coin", "swap", "fi", "dao", "protocol"}
            if clean_name not in skip_names and len(clean_name) > 2:
                queries.append(f"{token_name} cryptocurrency")
        # Search by symbol if meaningful
        if token_symbol and len(token_symbol) > 2:
            queries.append(f"{token_symbol} crypto blockchain")

        results = []
        for query in queries[:2]:
            data = self._get("/search/repositories", params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": 5,
            })
            if data and "items" in data:
                results.extend(data["items"])

        return results

    def get_repo_activity(self, owner: str, repo: str) -> dict:
        """Get activity metrics for a specific repo."""
        repo_data = self._get(f"/repos/{owner}/{repo}")
        if not repo_data:
            return {}

        # Get recent commits
        commits = self._get(f"/repos/{owner}/{repo}/commits", params={"per_page": 10})
        recent_commits = 0
        if isinstance(commits, list):
            cutoff = now_utc() - timedelta(days=30)
            for commit in commits:
                commit_date = commit.get("commit", {}).get("author", {}).get("date", "")
                if commit_date:
                    try:
                        dt = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
                        if dt > cutoff:
                            recent_commits += 1
                    except (ValueError, TypeError):
                        pass

        return {
            "name": repo_data.get("full_name", ""),
            "stars": repo_data.get("stargazers_count", 0),
            "forks": repo_data.get("forks_count", 0),
            "watchers": repo_data.get("subscribers_count", 0),
            "open_issues": repo_data.get("open_issues_count", 0),
            "last_push": repo_data.get("pushed_at", ""),
            "created": repo_data.get("created_at", ""),
            "language": repo_data.get("language", ""),
            "description": repo_data.get("description", ""),
            "recent_commits_30d": recent_commits,
            "archived": repo_data.get("archived", False),
        }

    def score_project(self, token_name: str, token_symbol: str = "") -> dict:
        """Score a token's GitHub presence. Returns {score: 0-10, details: {}}."""
        repos = self.search_repos(token_name, token_symbol)

        if not repos:
            return {"score": 0, "repos_found": 0, "details": "No repos found"}

        best_repo = None
        best_score = 0

        for repo in repos[:5]:
            owner = repo.get("owner", {}).get("login", "")
            name = repo.get("name", "")
            if not owner or not name:
                continue

            activity = self.get_repo_activity(owner, name)
            if not activity:
                continue

            # Calculate repo quality score
            score = 0
            stars = activity.get("stars", 0)
            forks = activity.get("forks", 0)
            commits = activity.get("recent_commits_30d", 0)
            archived = activity.get("archived", False)

            if archived:
                continue

            # Stars scoring
            if stars >= 1000:
                score += 3.0
            elif stars >= 100:
                score += 2.0
            elif stars >= 10:
                score += 1.0

            # Forks
            if forks >= 100:
                score += 2.0
            elif forks >= 10:
                score += 1.0

            # Recent activity
            if commits >= 10:
                score += 3.0
            elif commits >= 5:
                score += 2.0
            elif commits >= 1:
                score += 1.0

            # Language bonus (Rust for Solana, Solidity for EVM)
            lang = activity.get("language", "")
            if lang in ("Rust", "Solidity", "TypeScript", "Move"):
                score += 1.0

            if score > best_score:
                best_score = score
                best_repo = activity

        final_score = min(10.0, best_score)
        return {
            "score": round(final_score, 1),
            "repos_found": len(repos),
            "best_repo": best_repo,
            "details": best_repo.get("name", "none") if best_repo else "No active repos",
        }


# ─── Jupiter New Token Detection (Solana-specific, FREE) ─────────────────

class JupiterTokenScanner:
    """Monitor Jupiter for new Solana tokens. Completely free, no API key needed."""

    def __init__(self):
        self._last_request = 0.0
        self._token_cache = {}
        self._cache_time = 0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < config_alpha.JUPITER_DELAY:
            time.sleep(config_alpha.JUPITER_DELAY - elapsed)
        self._last_request = time.time()

    def get_token_price(self, token_address: str) -> float:
        """Get current price for a Solana token via Jupiter."""
        self._rate_limit()
        url = f"{config_alpha.JUPITER_PRICE_API}?ids={token_address}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                token_data = data.get("data", {}).get(token_address, {})
                return safe_float(token_data.get("price"))
        except Exception as e:
            log.warning(f"Jupiter price failed for {token_address[:10]}...: {e}")
            return 0

    def get_multiple_prices(self, addresses: list[str]) -> dict:
        """Get prices for multiple tokens at once."""
        if not addresses:
            return {}
        self._rate_limit()
        ids = ",".join(addresses[:100])
        url = f"{config_alpha.JUPITER_PRICE_API}?ids={ids}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                prices = {}
                for addr, info in data.get("data", {}).items():
                    prices[addr] = safe_float(info.get("price"))
                return prices
        except Exception as e:
            log.warning(f"Jupiter batch prices failed: {e}")
            return {}


# ─── Social Intelligence Aggregator ──────────────────────────────────────

class SocialIntel:
    """
    Aggregates social signals from multiple sources into a unified score.
    Sources: CryptoPanic (news), GitHub (dev activity), DexScreener (socials).
    """

    def __init__(self):
        self.cryptopanic = CryptoPanicClient()
        self.github = GitHubMonitor()
        self.jupiter = JupiterTokenScanner()
        self._cache = {}
        self._cache_ttl = 600  # 10 min cache

    def analyze_token(self, token: dict) -> dict:
        """
        Analyze social signals for a token candidate.
        Returns enriched token dict with social_intel_* fields.
        """
        name = token.get("name", "").split("/")[0].strip()
        symbol = token.get("symbol", token.get("token_symbol", ""))
        address = token.get("address", "")

        # Check cache
        cache_key = address or name
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                token.update(cached)
                return token

        result = {
            "social_intel_score": 5.0,
            "social_intel_signals": [],
            "news_sentiment": None,
            "github_score": 0,
            "news_count": 0,
        }

        # 1. News sentiment (CryptoPanic)
        if config_alpha.CRYPTOPANIC_API_KEY and symbol:
            news = self.cryptopanic.search_token_news(symbol)
            if news:
                sentiment = self.cryptopanic.get_news_sentiment(news)
                result["news_sentiment"] = sentiment
                result["news_count"] = sentiment["total"]
                if sentiment["total"] >= 3:
                    result["social_intel_signals"].append(f"news_mentions_{sentiment['total']}")
                    if sentiment["score"] >= 7:
                        result["social_intel_signals"].append("news_bullish")
                    elif sentiment["score"] <= 3:
                        result["social_intel_signals"].append("news_bearish")

        # 2. GitHub activity (only for tokens with meaningful names)
        if name and len(name) > 3:
            gh = self.github.score_project(name, symbol)
            result["github_score"] = gh.get("score", 0)
            if gh["score"] >= 3:
                result["social_intel_signals"].append(f"github_active_{gh['score']:.0f}")
            if gh["score"] >= 6:
                result["social_intel_signals"].append("github_strong")

        # 3. Calculate composite social intel score
        scores = []

        # News component (0-10)
        if result["news_sentiment"]:
            news_score = result["news_sentiment"]["score"]
            # Boost if many mentions
            if result["news_count"] >= 5:
                news_score = min(10, news_score + 1)
            scores.append(("news", news_score, 0.4))
        else:
            scores.append(("news", 5.0, 0.2))  # neutral if no data, lower weight

        # GitHub component (0-10)
        if result["github_score"] > 0:
            scores.append(("github", result["github_score"], 0.3))
        else:
            scores.append(("github", 3.0, 0.1))  # slightly negative if no repo

        # DexScreener social presence (from existing token data)
        social_score = 5.0
        has_website = token.get("has_website") or any(
            s for s in token.get("narrator_signals", []) if "website" in s
        )
        has_twitter = token.get("has_twitter") or any(
            s for s in token.get("narrator_signals", []) if "twitter" in s
        )
        has_telegram = token.get("has_telegram") or any(
            s for s in token.get("narrator_signals", []) if "telegram" in s
        )
        if has_website:
            social_score += 1.5
        if has_twitter:
            social_score += 1.5
        if has_telegram:
            social_score += 1.0
        social_score = min(10, social_score)
        scores.append(("dex_social", social_score, 0.3))

        # Weighted average
        total_weight = sum(w for _, _, w in scores)
        if total_weight > 0:
            result["social_intel_score"] = round(
                sum(s * w for _, s, w in scores) / total_weight, 1
            )

        # Cache result
        self._cache[cache_key] = (time.time(), result)

        token.update(result)
        return token

    def analyze_batch(self, tokens: list[dict]) -> list[dict]:
        """Analyze social signals for a batch of token candidates."""
        log.info(f"Analyzing social intelligence for {len(tokens)} tokens...")
        analyzed = []
        for i, token in enumerate(tokens):
            try:
                token = self.analyze_token(token)
                if token.get("social_intel_signals"):
                    log.info(f"  [{i+1}/{len(tokens)}] {token.get('name', '?')}: "
                             f"score={token.get('social_intel_score', 0):.1f}, "
                             f"signals={token.get('social_intel_signals')}")
            except Exception as e:
                log.warning(f"  Social intel failed for {token.get('name', '?')}: {e}")
                token["social_intel_score"] = 5.0
                token["social_intel_signals"] = []
            analyzed.append(token)
        return analyzed

    def get_trending_narratives(self) -> list[dict]:
        """Get currently trending crypto narratives from news."""
        if not config_alpha.CRYPTOPANIC_API_KEY:
            log.warning("CryptoPanic API key not set. Skipping news analysis.")
            return []

        hot_news = self.cryptopanic.get_trending_news("hot")
        rising_news = self.cryptopanic.get_trending_news("rising")

        all_news = hot_news + rising_news
        if not all_news:
            return []

        # Extract mentioned tokens
        mentions = self.cryptopanic.extract_mentioned_tokens(all_news)
        sentiment = self.cryptopanic.get_news_sentiment(all_news)

        narratives = []
        for symbol, count in list(mentions.items())[:10]:
            token_news = self.cryptopanic.search_token_news(symbol)
            token_sentiment = self.cryptopanic.get_news_sentiment(token_news)

            narratives.append({
                "symbol": symbol,
                "mention_count": count,
                "sentiment_score": token_sentiment["score"],
                "positive": token_sentiment["positive"],
                "negative": token_sentiment["negative"],
            })

        return narratives


# ─── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Social Intelligence Module")
    parser.add_argument("--scan", action="store_true", help="Scan for social signals")
    parser.add_argument("--news", action="store_true", help="Get trending crypto news")
    parser.add_argument("--github", type=str, help="Check GitHub activity for a token name")
    parser.add_argument("--narratives", action="store_true", help="Get trending narratives")

    args = parser.parse_args()
    setup_logging()

    intel = SocialIntel()

    if args.news:
        if not config_alpha.CRYPTOPANIC_API_KEY:
            print("Set CRYPTOPANIC_API_KEY environment variable first.")
            print("Sign up at: https://cryptopanic.com/developers/api/")
            return

        print("Fetching trending crypto news...")
        hot = intel.cryptopanic.get_trending_news("hot")
        print(f"\nHot News ({len(hot)} items):")
        for item in hot[:10]:
            title = item.get("title", "?")
            source = item.get("source", {}).get("title", "?")
            votes = item.get("votes", {})
            pos = votes.get("positive", 0)
            neg = votes.get("negative", 0)
            print(f"  [{pos}+/{neg}-] {title} ({source})")

        mentions = intel.cryptopanic.extract_mentioned_tokens(hot)
        if mentions:
            print(f"\nMost mentioned tokens:")
            for sym, count in list(mentions.items())[:10]:
                print(f"  {sym}: {count} mentions")
        return

    if args.github:
        print(f"Checking GitHub activity for: {args.github}")
        result = intel.github.score_project(args.github)
        print(f"\nGitHub Score: {result['score']}/10")
        print(f"Repos found: {result['repos_found']}")
        if result.get("best_repo"):
            repo = result["best_repo"]
            print(f"Best repo: {repo['name']}")
            print(f"  Stars: {repo['stars']}, Forks: {repo['forks']}")
            print(f"  Recent commits (30d): {repo['recent_commits_30d']}")
            print(f"  Language: {repo['language']}")
            print(f"  Last push: {repo['last_push']}")
        return

    if args.narratives:
        if not config_alpha.CRYPTOPANIC_API_KEY:
            print("Set CRYPTOPANIC_API_KEY for narrative detection.")
            return
        narratives = intel.get_trending_narratives()
        print(f"\nTrending Narratives ({len(narratives)}):")
        for n in narratives:
            emoji = "+" if n["sentiment_score"] >= 6 else ("-" if n["sentiment_score"] <= 4 else "~")
            print(f"  [{emoji}] {n['symbol']}: {n['mention_count']} mentions, "
                  f"sentiment {n['sentiment_score']:.1f}/10 "
                  f"(+{n['positive']}/-{n['negative']})")
        return

    if args.scan:
        # Quick demo: analyze a sample token
        sample_token = {
            "name": "Example Token",
            "symbol": "EX",
            "address": "example",
            "narrator_signals": [],
        }
        result = intel.analyze_token(sample_token)
        print(f"Social Intel Score: {result.get('social_intel_score', 0)}/10")
        print(f"Signals: {result.get('social_intel_signals', [])}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
