#!/usr/bin/env python3
"""
Smart Wallet Tracker - Follow profitable wallets in real-time.

Tracks wallets on Solana (via Helius) and EVM chains (via Etherscan/Basescan).
Detects new token buys and generates alpha signals.

v2.1: ThreadPoolExecutor for parallel scanning, per-wallet caching,
      Helius→RPC fallback, timing metrics. 22 wallets in <3 minutes.

Usage:
  python3 alpha/smart_wallet_tracker.py --monitor          # continuous monitoring
  python3 alpha/smart_wallet_tracker.py --scan-once        # single scan
  python3 alpha/smart_wallet_tracker.py --discover         # find smart wallets from recent winners
  python3 alpha/smart_wallet_tracker.py --add-wallet <addr> --chain solana --label "trader1"
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import config_alpha
from utils import get_logger, setup_logging, load_json, save_json, now_utc, safe_float
from api_client import APIClient, DexScreenerClient

log = get_logger("wallet_tracker")

# ─── Per-wallet Response Cache ───────────────────────────────────────────
WALLET_CACHE_TTL = 300  # 5 minutes

_wallet_cache = {}  # {wallet_addr: (timestamp, signals_list)}


def _get_cached_wallet(address: str) -> list | None:
    """Return cached signals for a wallet if still fresh."""
    if address in _wallet_cache:
        ts, data = _wallet_cache[address]
        if time.time() - ts < WALLET_CACHE_TTL:
            log.debug(f"Cache hit for {address[:10]}...")
            return data
        del _wallet_cache[address]
    return None


def _set_cached_wallet(address: str, signals: list):
    _wallet_cache[address] = (time.time(), signals)


# ─── Helius Client (Solana Enhanced Transactions) ─────────────────────────

class HeliusClient(APIClient):
    """Helius API for parsed Solana transactions.

    Uses shorter timeouts and fewer retries than default APIClient
    so we fail fast and fallback to Solana RPC quickly.
    """

    def __init__(self):
        api_key = config_alpha.HELIUS_API_KEY
        base = f"{config_alpha.HELIUS_API_URL}/v0"
        super().__init__(base, config_alpha.HELIUS_DELAY, "helius")
        self.api_key = api_key
        # Override session with aggressive timeout/retry settings
        self.session = self._build_fast_session()
        if not api_key:
            log.warning("HELIUS_API_KEY not set. Smart wallet tracking on Solana will be limited.")

    def _build_fast_session(self):
        """Build a session with short timeout and 1 retry (not 3)."""
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        session = requests.Session()
        retry = Retry(
            total=1,  # only 1 retry (not 3) - fail fast, use RPC fallback
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "CryptoSwarm/2.1",
        })
        return session

    def get(self, endpoint, params=None, use_cache=True):
        """Override with 5s timeout instead of default 10s."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        cache_key = self._get_cache_key(url, params)

        if use_cache:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

        self._rate_limit()
        self._request_count += 1
        try:
            resp = self.session.get(url, params=params, timeout=5)  # 5s not 10s
            if resp.status_code == 429:
                return None
            resp.raise_for_status()
            data = resp.json()
            if use_cache:
                self._set_cached(cache_key, data)
            return data
        except Exception as e:
            log.debug(f"[helius] Request failed ({e.__class__.__name__}): {endpoint}")
            return None

    def get_transactions(self, address: str, limit: int = 20, tx_type: str = "SWAP") -> list:
        """Get parsed transactions for a wallet, filtered by type."""
        if not self.api_key:
            return []
        params = {"api-key": self.api_key, "type": tx_type}
        data = self.get(f"/addresses/{address}/transactions", params=params, use_cache=False)
        if isinstance(data, list):
            return data[:limit]
        return []

    def get_signatures(self, address: str, limit: int = 10) -> list:
        """Get recent transaction signatures (cheap RPC call, saves credits)."""
        result = self._rpc_call("getSignaturesForAddress",
                                [address, {"limit": limit}])
        return result if isinstance(result, list) else []

    def _rpc_call(self, method: str, params: list):
        """JSON-RPC call via Helius RPC endpoint (cheaper than parsed API)."""
        if not self.api_key:
            return None
        import urllib.request
        rpc_url = config_alpha.HELIUS_RPC_URL
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }).encode()
        req = urllib.request.Request(
            rpc_url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
                return result.get("result")
        except Exception as e:
            log.debug(f"[helius] RPC {method} failed: {e}")
            return None

    def get_token_metadata(self, mint_addresses: list[str]) -> list:
        """Get metadata for token mint addresses."""
        if not self.api_key:
            return []
        import urllib.request
        url = f"{config_alpha.HELIUS_API_URL}/v0/token-metadata?api-key={self.api_key}"
        payload = json.dumps({"mintAccounts": mint_addresses[:100]}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.warning(f"Token metadata failed: {e}")
            return []


# ─── Solana RPC Client (Free fallback) ────────────────────────────────────

class SolanaRPCClient:
    """Direct Solana RPC for basic wallet queries when Helius isn't available."""

    def __init__(self):
        self.rpc_url = config_alpha.SOLANA_RPC_URL
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < config_alpha.SOLANA_RPC_DELAY:
            time.sleep(config_alpha.SOLANA_RPC_DELAY - elapsed)
        self._last_request = time.time()

    def _rpc_call(self, method: str, params: list) -> dict | None:
        import urllib.request
        self._rate_limit()
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }).encode()
        req = urllib.request.Request(
            self.rpc_url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = json.loads(resp.read())
                return result.get("result")
        except Exception as e:
            log.warning(f"Solana RPC {method} failed: {e}")
            return None

    def get_signatures(self, address: str, limit: int = 20) -> list:
        """Get recent transaction signatures for a wallet."""
        result = self._rpc_call("getSignaturesForAddress", [address, {"limit": limit}])
        return result if isinstance(result, list) else []

    def get_token_largest_accounts(self, mint: str) -> list:
        """Get top holders for a SPL token (free RPC call)."""
        result = self._rpc_call("getTokenLargestAccounts", [mint])
        if result and "value" in result:
            return result["value"]
        return []

    def get_token_accounts(self, address: str) -> list:
        """Get all SPL token accounts owned by a wallet."""
        result = self._rpc_call("getTokenAccountsByOwner", [
            address,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ])
        if result and "value" in result:
            return result["value"]
        return []


# ─── EVM Scanner Client (Etherscan/Basescan) ─────────────────────────────

class EVMScannerClient:
    """Etherscan/Basescan API for EVM wallet tracking."""

    def __init__(self, chain: str = "base"):
        if chain == "base":
            self.base_url = config_alpha.BASESCAN_BASE
            self.api_key = config_alpha.BASESCAN_API_KEY
        else:
            self.base_url = config_alpha.ETHERSCAN_BASE
            self.api_key = config_alpha.ETHERSCAN_API_KEY
        self.chain = chain
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < 0.25:  # 4 req/sec max
            time.sleep(0.25 - elapsed)
        self._last_request = time.time()

    def get_token_transfers(self, address: str, start_block: int = 0) -> list:
        """Get ERC-20 token transfers for a wallet."""
        if not self.api_key:
            return []
        self._rate_limit()
        import urllib.request
        url = (
            f"{self.base_url}?module=account&action=tokentx"
            f"&address={address}&startblock={start_block}&sort=desc"
            f"&apikey={self.api_key}"
        )
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read())
                if data.get("status") == "1":
                    return data.get("result", [])[:50]
        except Exception as e:
            log.warning(f"[{self.chain}] Token transfers failed for {address[:10]}...: {e}")
        return []


# ─── Smart Wallets Database ──────────────────────────────────────────────

class WalletDB:
    """Manages the database of tracked smart wallets and their trade history."""

    def __init__(self):
        self.wallets_file = config_alpha.WALLETS_DB_FILE
        self.trades_file = config_alpha.WALLET_TRADES_FILE
        os.makedirs(os.path.dirname(self.wallets_file), exist_ok=True)

    def load_wallets(self) -> dict:
        """Load tracked wallets. Format: {address: {chain, label, added, stats}}"""
        data = load_json(self.wallets_file)
        if not isinstance(data, dict):
            data = {}
        # Merge in hardcoded wallets from config
        for addr, label in config_alpha.SMART_WALLETS_SOLANA.items():
            if addr not in data:
                data[addr] = {
                    "chain": "solana", "label": label,
                    "added": now_utc().isoformat(),
                    "trades": 0, "wins": 0, "total_pnl_pct": 0,
                }
        for addr, label in config_alpha.SMART_WALLETS_EVM.items():
            if addr not in data:
                data[addr] = {
                    "chain": "base", "label": label,
                    "added": now_utc().isoformat(),
                    "trades": 0, "wins": 0, "total_pnl_pct": 0,
                }
        return data

    def save_wallets(self, wallets: dict):
        save_json(self.wallets_file, wallets)

    def add_wallet(self, address: str, chain: str = "solana", label: str = "",
                   notes: str = "", pnl: float = 0, win_rate: float = 0):
        wallets = self.load_wallets()
        if address in wallets:
            log.info(f"Wallet already tracked: {address[:10]}...")
            return False
        wallets[address] = {
            "chain": chain, "label": label or f"wallet_{len(wallets)+1}",
            "added": now_utc().isoformat(),
            "trades": 0, "wins": 0, "total_pnl_pct": pnl,
            "win_rate_discovered": win_rate,
            "notes": notes,
        }
        self.save_wallets(wallets)
        log.info(f"Added wallet: {address[:10]}... ({chain}) - {label}")
        return True

    def remove_inactive_wallets(self, max_age_days: int = 7) -> int:
        """Remove wallets with 0 trades that are older than max_age_days."""
        wallets = self.load_wallets()
        cutoff = now_utc() - timedelta(days=max_age_days)
        to_remove = []
        for addr, info in wallets.items():
            if info.get("trades", 0) == 0:
                try:
                    added = datetime.fromisoformat(info.get("added", ""))
                    if added < cutoff:
                        to_remove.append(addr)
                except (ValueError, TypeError):
                    to_remove.append(addr)
        for addr in to_remove:
            del wallets[addr]
        if to_remove:
            self.save_wallets(wallets)
            log.info(f"Removed {len(to_remove)} inactive wallets (0 trades, >{max_age_days}d old)")
        return len(to_remove)

    def load_recent_trades(self) -> list:
        data = load_json(self.trades_file)
        return data if isinstance(data, list) else []

    def save_trade(self, trade: dict):
        trades = self.load_recent_trades()
        trades.append(trade)
        # Keep last 1000 trades
        if len(trades) > 1000:
            trades = trades[-1000:]
        save_json(self.trades_file, trades)


# ─── Helius WebSocket (Real-time Solana Wallet Monitoring) ────────────────

class HeliusWebSocket:
    """Real-time Solana wallet monitoring via Helius Enhanced WebSocket.

    Subscribes to transactionSubscribe for all tracked Solana wallets.
    Fires callback on each buy event. Auto-reconnects with exponential backoff.
    """

    def __init__(self, wallet_addresses: list[str], on_buy_callback: callable):
        self.addresses = wallet_addresses
        self.on_buy = on_buy_callback
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._connected = False
        self._reconnect_delay = config_alpha.WS_RECONNECT_BASE_DELAY
        self.ws_failed = False  # True if WS permanently failed (e.g. 403)

    def start(self):
        """Start WebSocket listener in daemon thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="helius-ws")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    @property
    def is_connected(self):
        return self._connected

    def _run_loop(self):
        """Main loop: connect, subscribe, listen. Reconnect on failure."""
        while not self._stop.is_set():
            try:
                self._connect_and_listen()
            except Exception as e:
                if self.ws_failed:
                    break
                log.warning(f"[WS] Disconnected: {e}")
                self._connected = False
            if self.ws_failed:
                break
            if not self._stop.is_set():
                log.info(f"[WS] Reconnecting in {self._reconnect_delay}s...")
                self._stop.wait(timeout=self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, config_alpha.WS_RECONNECT_MAX_DELAY)

    def _connect_and_listen(self):
        import websocket as ws_lib

        ws_url = config_alpha.HELIUS_WS_URL
        if not ws_url:
            log.warning("[WS] No HELIUS_API_KEY, cannot start WebSocket")
            self._stop.wait(timeout=60)
            return

        ws = ws_lib.WebSocket()
        try:
            ws.connect(ws_url, timeout=10)
        except Exception as e:
            err_str = str(e)
            if "403" in err_str or "Forbidden" in err_str:
                log.warning("[WS] Helius WebSocket returned 403 (requires Business plan). "
                            "Disabling WebSocket permanently for this session.")
                self.ws_failed = True
                self._stop.set()
                return
            raise
        self._ws = ws
        self._connected = True
        self._reconnect_delay = config_alpha.WS_RECONNECT_BASE_DELAY  # reset on success
        log.info(f"[WS] Connected to Helius WebSocket ({len(self.addresses)} wallets)")

        # Subscribe to transactions for tracked wallets
        sub_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {"accountInclude": self.addresses},
                {"commitment": "confirmed", "encoding": "jsonParsed",
                 "transactionDetails": "full", "maxSupportedTransactionVersion": 0}
            ]
        })
        ws.send(sub_msg)
        ws.settimeout(config_alpha.WS_PING_INTERVAL + 10)

        while not self._stop.is_set():
            try:
                raw = ws.recv()
                if not raw:
                    break
                data = json.loads(raw)
                self._handle_message(data)
            except ws_lib.WebSocketTimeoutException:
                # Send ping to keep alive
                ws.ping()
            except Exception as e:
                log.warning(f"[WS] Receive error: {e}")
                break

        ws.close()
        self._connected = False

    def _handle_message(self, data: dict):
        """Parse WS message and fire callback if it's a buy."""
        params = data.get("params", {})
        result = params.get("result", {})
        tx = result.get("transaction", {})
        if not tx:
            return
        self.on_buy(tx)


# ─── Smart Wallet Tracker Core ────────────────────────────────────────────

# Max parallel wallet scans
MAX_WORKERS = 5


class SmartWalletTracker:
    """
    Monitors smart wallets for new token purchases.
    Generates alpha signals when tracked wallets buy new/small tokens.

    v2.1: Parallel scanning with ThreadPoolExecutor + per-wallet caching.
    """

    def __init__(self):
        self.db = WalletDB()
        self.helius = HeliusClient()
        self.solana_rpc = SolanaRPCClient()
        self.dexscreener = DexScreenerClient()
        self._seen_txs = set()  # avoid duplicate alerts
        self._known_signatures: dict[str, set] = {}  # {wallet_addr: set of sig hashes}
        self._ws = None  # HeliusWebSocket instance
        self._ws_callback = None  # external callback for WS events

    def start_websocket(self, on_signal_callback: callable):
        """Start real-time WebSocket monitoring for Solana wallets."""
        wallets = self.db.load_wallets()
        sol_addrs = [a for a, w in wallets.items() if w.get("chain") == "solana"]
        if not sol_addrs:
            log.info("[WS] No Solana wallets to monitor")
            return
        if not config_alpha.HELIUS_WS_URL:
            log.info("[WS] No HELIUS_API_KEY, skipping WebSocket")
            return

        self._ws_callback = on_signal_callback
        self._ws = HeliusWebSocket(sol_addrs, self._on_ws_event)
        self._ws.start()
        log.info(f"[WS] Started monitoring {len(sol_addrs)} Solana wallets")

    def stop_websocket(self):
        """Stop WebSocket monitoring."""
        if self._ws:
            self._ws.stop()
            self._ws = None

    @property
    def ws_connected(self) -> bool:
        return self._ws is not None and self._ws.is_connected

    @property
    def ws_failed(self) -> bool:
        return self._ws is not None and self._ws.ws_failed

    def _on_ws_event(self, tx: dict):
        """Handle a raw WebSocket transaction event."""
        try:
            # Try to parse as a swap using existing logic
            # WS enhanced tx format has similar fields to Helius HTTP API
            for addr, info in self.db.load_wallets().items():
                if info.get("chain") != "solana":
                    continue
                signal = self._parse_helius_swap(tx, addr, info.get("label", addr[:10]))
                if signal and signal["tx_sig"] not in self._seen_txs:
                    self._seen_txs.add(signal["tx_sig"])
                    enriched = self.enrich_signals([signal])
                    if enriched and self._ws_callback:
                        self._ws_callback(enriched[0])
                    return
        except Exception as e:
            log.debug(f"[WS] Event parse error: {e}")

    def scan_all_wallets(self) -> list[dict]:
        """
        Scan all tracked wallets for recent new token buys.
        Uses ThreadPoolExecutor for parallel scanning.
        Returns list of alpha signals.
        """
        wallets = self.db.load_wallets()
        if not wallets:
            log.warning("No wallets tracked. Use --add-wallet or --discover to add wallets.")
            return []

        start_time = time.time()
        log.info(f"Scanning {len(wallets)} tracked wallets (parallel, max {MAX_WORKERS} workers)...")
        all_signals = []
        errors = 0
        cached_hits = 0

        solana_wallets = {a: w for a, w in wallets.items() if w.get("chain") == "solana"}
        evm_wallets = {a: w for a, w in wallets.items() if w.get("chain") in ("base", "ethereum")}

        # ── Parallel scan: Solana wallets ──
        if solana_wallets:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {}
                for addr, wallet_info in solana_wallets.items():
                    # Check cache first
                    cached = _get_cached_wallet(addr)
                    if cached is not None:
                        all_signals.extend(cached)
                        cached_hits += 1
                        continue
                    future = executor.submit(self._scan_solana_wallet_safe, addr, wallet_info)
                    futures[future] = addr

                for future in as_completed(futures, timeout=120):
                    addr = futures[future]
                    try:
                        signals = future.result(timeout=30)
                        _set_cached_wallet(addr, signals)
                        all_signals.extend(signals)
                    except Exception as e:
                        errors += 1
                        log.warning(f"Wallet {addr[:10]}... scan failed: {e}")

        # ── Parallel scan: EVM wallets ──
        if evm_wallets:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {}
                for addr, wallet_info in evm_wallets.items():
                    cached = _get_cached_wallet(addr)
                    if cached is not None:
                        all_signals.extend(cached)
                        cached_hits += 1
                        continue
                    future = executor.submit(self._scan_evm_wallet_safe, addr, wallet_info)
                    futures[future] = addr

                for future in as_completed(futures, timeout=120):
                    addr = futures[future]
                    try:
                        signals = future.result(timeout=30)
                        _set_cached_wallet(addr, signals)
                        all_signals.extend(signals)
                    except Exception as e:
                        errors += 1
                        log.warning(f"EVM wallet {addr[:10]}... scan failed: {e}")

        elapsed = time.time() - start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)

        if all_signals:
            log.info(f"Found {len(all_signals)} new buy signals from smart wallets!")
        else:
            log.info("No new buys detected from tracked wallets.")

        log.info(
            f"[wallet_tracker] Scan completed in {minutes}m {seconds}s "
            f"({len(wallets)} wallets, {cached_hits} cached, {errors} errors)"
        )

        return all_signals

    def _scan_solana_wallet_safe(self, address: str, wallet_info: dict) -> list[dict]:
        """Thread-safe wrapper with Helius → RPC fallback."""
        try:
            return self._scan_solana_wallet(address, wallet_info)
        except Exception as e:
            log.warning(f"Solana wallet {address[:10]}... error: {e}")
            return []

    def _scan_evm_wallet_safe(self, address: str, wallet_info: dict) -> list[dict]:
        """Thread-safe wrapper for EVM scanning."""
        try:
            return self._scan_evm_wallet(address, wallet_info)
        except Exception as e:
            log.warning(f"EVM wallet {address[:10]}... error: {e}")
            return []

    def _scan_solana_wallet(self, address: str, wallet_info: dict) -> list[dict]:
        """Scan a single Solana wallet for recent swaps. Helius → RPC fallback.

        v5.0: Signatures-first strategy. Calls cheap getSignaturesForAddress RPC
        first, and only fetches full parsed transactions if there are new signatures.
        Saves Helius credits when wallets have no new activity.
        """
        label = wallet_info.get("label", address[:10])
        signals = []

        # Strategy 1: Signatures-first (cheap RPC) then parse only new ones
        if config_alpha.HELIUS_API_KEY:
            try:
                # Step 1: Get recent signatures (cheap RPC call)
                sigs = self.helius.get_signatures(address, limit=10)
                known = self._known_signatures.get(address, set())
                new_sigs = [s for s in sigs if s.get("signature") not in known
                            and s.get("signature") not in self._seen_txs]

                # Update known signatures for next cycle
                if sigs:
                    self._known_signatures[address] = {s.get("signature") for s in sigs}

                if not new_sigs:
                    return signals  # No new activity, saved credits!

                # Step 2: Only fetch full parsed txs (expensive) for new signatures
                log.debug(f"  {label}: {len(new_sigs)} new sigs, fetching parsed txs")
                txs = self.helius.get_transactions(address, limit=len(new_sigs), tx_type="SWAP")
                for tx in txs:
                    signal = self._parse_helius_swap(tx, address, label)
                    if signal and signal["tx_sig"] not in self._seen_txs:
                        self._seen_txs.add(signal["tx_sig"])
                        signals.append(signal)
                if txs is not None:
                    return signals
            except Exception as e:
                log.debug(f"Helius failed for {label}, falling back to RPC: {e}")

        # Strategy 2: Solana RPC fallback (free, always available)
        try:
            token_accounts = self.solana_rpc.get_token_accounts(address)
            for account in token_accounts:
                parsed = account.get("account", {}).get("data", {}).get("parsed", {})
                info = parsed.get("info", {})
                mint = info.get("mint", "")
                amount = safe_float(info.get("tokenAmount", {}).get("uiAmount"))
                if mint and amount > 0:
                    signal = self._check_token_freshness(mint, address, label, "solana")
                    if signal:
                        signals.append(signal)
        except Exception as e:
            log.warning(f"RPC fallback also failed for {label}: {e}")

        return signals

    def _parse_helius_swap(self, tx: dict, wallet_addr: str, wallet_label: str) -> dict | None:
        """Parse a Helius enhanced swap transaction into an alpha signal."""
        sig = tx.get("signature", "")
        ts = tx.get("timestamp")
        if not ts:
            return None

        # Check if transaction is recent enough
        tx_time = datetime.fromtimestamp(ts, tz=timezone.utc)
        hours_old = (now_utc() - tx_time).total_seconds() / 3600
        if hours_old > config_alpha.WALLET_LOOKBACK_HOURS:
            return None

        # Parse token transfers to find what was bought
        token_transfers = tx.get("tokenTransfers", [])
        native_transfers = tx.get("nativeTransfers", [])

        bought_token = None
        bought_amount = 0
        spent_sol = 0

        for transfer in token_transfers:
            to_account = transfer.get("toUserAccount", "")
            mint = transfer.get("mint", "")
            amount = safe_float(transfer.get("tokenAmount"))

            if to_account == wallet_addr and mint:
                # Wallet received tokens = BUY
                bought_token = mint
                bought_amount = amount

        # Check SOL spent
        for transfer in native_transfers:
            if transfer.get("fromUserAccount") == wallet_addr:
                spent_sol += safe_float(transfer.get("amount", 0)) / 1e9  # lamports to SOL

        if not bought_token:
            return None

        # Skip if it's a known stablecoin or wrapped SOL
        skip_mints = {
            "So11111111111111111111111111111111111111112",  # Wrapped SOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        }
        if bought_token in skip_mints:
            return None

        return {
            "type": "smart_wallet_buy",
            "tx_sig": sig,
            "wallet_address": wallet_addr,
            "wallet_label": wallet_label,
            "chain": "solana",
            "token_address": bought_token,
            "amount": bought_amount,
            "spent_sol": spent_sol,
            "timestamp": tx_time.isoformat(),
            "hours_ago": round(hours_old, 2),
        }

    def _scan_evm_wallet(self, address: str, wallet_info: dict) -> list[dict]:
        """Scan an EVM wallet for recent token transfers (buys)."""
        chain = wallet_info.get("chain", "base")
        label = wallet_info.get("label", address[:10])
        signals = []

        scanner = EVMScannerClient(chain)
        transfers = scanner.get_token_transfers(address)

        cutoff = now_utc() - timedelta(hours=config_alpha.WALLET_LOOKBACK_HOURS)

        for tx in transfers:
            # Only look at incoming transfers (buys)
            if tx.get("to", "").lower() != address.lower():
                continue

            tx_time = datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc)
            if tx_time < cutoff:
                continue

            token_addr = tx.get("contractAddress", "")
            token_name = tx.get("tokenName", "")
            token_symbol = tx.get("tokenSymbol", "")
            decimals = int(tx.get("tokenDecimal", 18))
            value = safe_float(tx.get("value", 0)) / (10 ** decimals)
            tx_hash = tx.get("hash", "")

            if tx_hash in self._seen_txs:
                continue
            self._seen_txs.add(tx_hash)

            # Skip known stablecoins/wrapped tokens
            skip_symbols = {"USDC", "USDT", "DAI", "WETH", "WBNB"}
            if token_symbol.upper() in skip_symbols:
                continue

            signals.append({
                "type": "smart_wallet_buy",
                "tx_sig": tx_hash,
                "wallet_address": address,
                "wallet_label": label,
                "chain": chain,
                "token_address": token_addr,
                "token_name": token_name,
                "token_symbol": token_symbol,
                "amount": value,
                "timestamp": tx_time.isoformat(),
                "hours_ago": round((now_utc() - tx_time).total_seconds() / 3600, 2),
            })

        return signals

    def _check_token_freshness(self, mint: str, wallet_addr: str,
                                wallet_label: str, chain: str) -> dict | None:
        """Check if a token is fresh enough to be an alpha signal."""
        # Use DexScreener to check token age and liquidity
        pairs = self.dexscreener.get_token_pairs(chain, mint)
        if not pairs:
            return None

        pair = pairs[0]
        created_at = pair.get("pairCreatedAt")
        if not created_at:
            return None

        pair_time = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
        age_hours = (now_utc() - pair_time).total_seconds() / 3600

        if age_hours > config_alpha.WALLET_NEW_TOKEN_MAX_AGE_HOURS:
            return None

        liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
        if liquidity < config.SCAN_MIN_LIQUIDITY:
            return None

        return {
            "type": "smart_wallet_buy",
            "tx_sig": f"rpc_{mint[:20]}_{int(time.time())}",
            "wallet_address": wallet_addr,
            "wallet_label": wallet_label,
            "chain": chain,
            "token_address": mint,
            "token_name": pair.get("baseToken", {}).get("name", "Unknown"),
            "token_symbol": pair.get("baseToken", {}).get("symbol", "?"),
            "liquidity_usd": liquidity,
            "pool_age_hours": round(age_hours, 1),
            "timestamp": now_utc().isoformat(),
            "hours_ago": 0,
        }

    def enrich_signals(self, signals: list[dict]) -> list[dict]:
        """Enrich buy signals with DexScreener data (price, liquidity, etc)."""
        if not signals:
            return []

        enriched = []
        # Group by chain for batch lookups
        by_chain = {}
        for sig in signals:
            chain = sig.get("chain", "solana")
            by_chain.setdefault(chain, []).append(sig)

        for chain, chain_signals in by_chain.items():
            # Batch lookup on DexScreener (up to 30 per call)
            addresses = list(set(s["token_address"] for s in chain_signals))
            dex_chain = config.DEXSCREENER_CHAINS.get(chain, chain)

            for i in range(0, len(addresses), 30):
                batch = addresses[i:i+30]
                pairs = self.dexscreener.get_tokens_batch(dex_chain, batch)

                # Build lookup map: token_address -> best pair
                pair_map = {}
                for pair in pairs:
                    addr = pair.get("baseToken", {}).get("address", "").lower()
                    if addr not in pair_map:
                        pair_map[addr] = pair
                    else:
                        # Keep pair with higher liquidity
                        existing_liq = safe_float(pair_map[addr].get("liquidity", {}).get("usd"))
                        new_liq = safe_float(pair.get("liquidity", {}).get("usd"))
                        if new_liq > existing_liq:
                            pair_map[addr] = pair

                for sig in chain_signals:
                    addr = sig["token_address"].lower()
                    pair = pair_map.get(addr)
                    if pair:
                        created_at = pair.get("pairCreatedAt")
                        age_hours = 0
                        if created_at:
                            pair_time = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
                            age_hours = (now_utc() - pair_time).total_seconds() / 3600

                        sig.update({
                            "token_name": pair.get("baseToken", {}).get("name", sig.get("token_name", "?")),
                            "token_symbol": pair.get("baseToken", {}).get("symbol", sig.get("token_symbol", "?")),
                            "price_usd": safe_float(pair.get("priceUsd")),
                            "liquidity_usd": safe_float(pair.get("liquidity", {}).get("usd")),
                            "volume_24h": safe_float(pair.get("volume", {}).get("h24")),
                            "mcap": safe_float(pair.get("marketCap")),
                            "price_change_24h": safe_float(pair.get("priceChange", {}).get("h24")),
                            "pool_address": pair.get("pairAddress"),
                            "pool_age_hours": round(age_hours, 1),
                            "dex_url": pair.get("url", ""),
                            "buys_24h": pair.get("txns", {}).get("h24", {}).get("buys", 0),
                            "sells_24h": pair.get("txns", {}).get("h24", {}).get("sells", 0),
                        })

                        # Check socials
                        info = pair.get("info", {})
                        sig["has_socials"] = bool(info.get("socials") or info.get("websites"))

                    enriched.append(sig)

        return enriched

    def discover_smart_wallets(self, recent_winners: list[dict] = None) -> list[dict]:
        """
        Discover new smart wallets by analyzing early buyers of recent winning tokens.
        Uses DexScreener to find tokens that pumped, then traces early buyers.
        """
        log.info("Discovering smart wallets from recent winning tokens...")

        if not recent_winners:
            # Use current portfolio winners or scan for recent pumps
            recent_winners = self._find_recent_winners()

        discovered = []
        for winner in recent_winners[:10]:
            addr = winner.get("address", "")
            chain = winner.get("chain", "solana")
            name = winner.get("name", "?")

            if chain != "solana" or not config_alpha.HELIUS_API_KEY:
                continue

            log.info(f"  Analyzing early buyers of {name}...")

            # This would require Helius parsed transaction history for the token
            # For now, log what we'd need
            log.info(f"    Token: {addr[:20]}... - Would need Helius transaction history")

        return discovered

    def _find_recent_winners(self) -> list[dict]:
        """Find tokens that recently pumped significantly (potential smart money targets)."""
        dex = self.dexscreener
        winners = []

        # Check boosted tokens - these often have recent pump activity
        boosted = dex.get_boosted_tokens()
        for token in boosted[:20]:
            chain = token.get("chainId", "")
            addr = token.get("tokenAddress", "")
            if chain in ("solana", "base", "ethereum") and addr:
                pairs = dex.get_token_pairs(chain, addr)
                for pair in pairs[:1]:
                    change_24h = safe_float(pair.get("priceChange", {}).get("h24"))
                    liq = safe_float(pair.get("liquidity", {}).get("usd"))
                    if change_24h > 200 and liq > 50000:
                        winners.append({
                            "name": pair.get("baseToken", {}).get("name", "?"),
                            "address": addr,
                            "chain": chain,
                            "price_change_24h": change_24h,
                            "liquidity_usd": liq,
                        })

        log.info(f"Found {len(winners)} recent winners for wallet discovery")
        return winners


# ─── Alert Formatting ─────────────────────────────────────────────────────

def format_wallet_alert(signal: dict) -> str:
    """Format a smart wallet buy signal for Telegram."""
    wallet_label = signal.get("wallet_label", "Unknown")
    token_name = signal.get("token_name", "Unknown")
    token_symbol = signal.get("token_symbol", "?")
    chain = signal.get("chain", "?").upper()
    price = signal.get("price_usd", 0)
    liquidity = signal.get("liquidity_usd", 0)
    volume = signal.get("volume_24h", 0)
    age_hours = signal.get("pool_age_hours", 0)
    change = signal.get("price_change_24h", 0)
    addr = signal.get("token_address", "")

    dex_url = signal.get("dex_url", f"https://dexscreener.com/{signal.get('chain', 'solana')}/{addr}")

    msg = (
        f"<b>SMART WALLET BUY</b>\n\n"
        f"Wallet: <b>{wallet_label}</b>\n"
        f"Bought: <b>{token_name}</b> ({token_symbol}) on {chain}\n"
        f"Price: ${price:.8f} ({change:+.1f}% 24h)\n"
        f"Liquidity: ${liquidity:,.0f}\n"
        f"Volume 24h: ${volume:,.0f}\n"
        f"Pool Age: {age_hours:.1f}h\n"
        f"\n<code>{addr}</code>\n"
        f"\n<a href=\"{dex_url}\">DexScreener</a>"
    )
    return msg


# ─── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Smart Wallet Tracker")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring mode")
    parser.add_argument("--scan-once", action="store_true", help="Single scan")
    parser.add_argument("--discover", action="store_true", help="Discover smart wallets")
    parser.add_argument("--add-wallet", type=str, help="Add a wallet address to track")
    parser.add_argument("--chain", type=str, default="solana", help="Chain for wallet (solana/base/ethereum)")
    parser.add_argument("--label", type=str, default="", help="Label for the wallet")
    parser.add_argument("--list", action="store_true", help="List tracked wallets")
    parser.add_argument("--cleanup", action="store_true", help="Remove inactive wallets (0 trades, >7d old)")
    parser.add_argument("--interval", type=int, default=config_alpha.WALLET_CHECK_INTERVAL,
                        help=f"Check interval in seconds (default: {config_alpha.WALLET_CHECK_INTERVAL})")

    args = parser.parse_args()
    setup_logging()

    tracker = SmartWalletTracker()

    if args.add_wallet:
        tracker.db.add_wallet(args.add_wallet, args.chain, args.label)
        return

    if args.cleanup:
        removed = tracker.db.remove_inactive_wallets()
        print(f"Removed {removed} inactive wallets.")
        return

    if args.list:
        wallets = tracker.db.load_wallets()
        if not wallets:
            print("No wallets tracked. Use --add-wallet to add one.")
            return
        print(f"\nTracked Wallets ({len(wallets)}):")
        print("-" * 80)
        for addr, info in wallets.items():
            print(f"  {info.get('chain', '?'):10s} {addr[:20]}...  {info.get('label', '')}")
            wins = info.get("wins", 0)
            trades = info.get("trades", 0)
            wr = (wins / trades * 100) if trades > 0 else 0
            print(f"             Trades: {trades}, Wins: {wins} ({wr:.0f}%)")
        return

    if args.discover:
        tracker.discover_smart_wallets()
        return

    if args.scan_once:
        signals = tracker.scan_all_wallets()
        signals = tracker.enrich_signals(signals)
        if signals:
            print(f"\n{len(signals)} buy signal(s) detected:")
            for s in signals:
                print(f"  [{s.get('chain')}] {s.get('wallet_label')} bought "
                      f"{s.get('token_name', '?')} ({s.get('token_symbol', '?')})")
                if s.get("liquidity_usd"):
                    print(f"    Liq: ${s['liquidity_usd']:,.0f} | "
                          f"Vol: ${s.get('volume_24h', 0):,.0f} | "
                          f"Age: {s.get('pool_age_hours', 0):.1f}h")
            # Save signals
            save_json(config_alpha.ALPHA_ALERTS_FILE, signals)
            print(f"\nSignals saved to: {config_alpha.ALPHA_ALERTS_FILE}")
        else:
            print("No new buy signals detected.")
        return

    if args.monitor:
        from alert_monitor import send_telegram
        log.info(f"Starting wallet monitor (interval: {args.interval}s)")
        send_telegram(
            "<b>Smart Wallet Monitor Started</b>\n"
            f"Tracking {len(tracker.db.load_wallets())} wallets\n"
            f"Check interval: {args.interval}s"
        )

        while True:
            try:
                signals = tracker.scan_all_wallets()
                signals = tracker.enrich_signals(signals)
                for sig in signals:
                    # Only alert on tokens with minimum liquidity
                    if sig.get("liquidity_usd", 0) >= config.SCAN_MIN_LIQUIDITY:
                        alert = format_wallet_alert(sig)
                        send_telegram(alert)
                        tracker.db.save_trade(sig)
                        log.info(f"ALERT: {sig.get('wallet_label')} bought {sig.get('token_name')}")
            except Exception as e:
                log.error(f"Monitor error: {e}", exc_info=True)

            time.sleep(args.interval)


if __name__ == "__main__":
    main()
