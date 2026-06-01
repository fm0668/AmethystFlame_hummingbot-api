import asyncio
import json
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig

from services.market_data_service import MarketDataService


BINANCE_FAPI_URL = "https://fapi.binance.com"
CANDLES_READY_TIMEOUT = 12.0
CANDLES_READY_POLL_INTERVAL = 1.0
ORDER_BOOK_READY_TIMEOUT = 20.0
OPEN_INTEREST_HISTORY_LIMIT = 48


def hb_pair_to_binance_symbol(trading_pair: str) -> str:
    return trading_pair.replace("-", "").upper()


def binance_symbol_to_hb_pair(symbol: str, quote_asset: str = "USDC") -> str:
    return f"{symbol[:-len(quote_asset)]}-{quote_asset}"


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


class USDCPerpMarketService:
    def __init__(self, market_data_service: MarketDataService):
        self._market_data_service = market_data_service

    async def get_universe(
        self,
        connector_name: str = "binance_perpetual",
        quote_asset: str = "USDC",
        max_pairs: int = 32,
        min_24h_quote_volume: float = 0,
    ) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            exchange_info, tickers = await asyncio.gather(
                self._fetch_json(session, f"{BINANCE_FAPI_URL}/fapi/v1/exchangeInfo"),
                self._fetch_json(session, f"{BINANCE_FAPI_URL}/fapi/v1/ticker/24hr"),
            )
        ticker_by_symbol = {item["symbol"]: item for item in tickers}
        markets = []
        for symbol_info in exchange_info.get("symbols", []):
            if symbol_info.get("quoteAsset") != quote_asset:
                continue
            if symbol_info.get("contractType") != "PERPETUAL" or symbol_info.get("status") != "TRADING":
                continue
            symbol = symbol_info["symbol"]
            ticker = ticker_by_symbol.get(symbol, {})
            quote_volume = safe_float(ticker.get("quoteVolume"), 0) or 0
            if quote_volume < min_24h_quote_volume:
                continue
            markets.append(
                {
                    "symbol": symbol,
                    "trading_pair": binance_symbol_to_hb_pair(symbol, quote_asset),
                    "base_asset": symbol_info.get("baseAsset"),
                    "quote_asset": quote_asset,
                    "quote_volume_24h": quote_volume,
                    "last_price": safe_float(ticker.get("lastPrice")),
                    "price_change_pct_24h": safe_float(ticker.get("priceChangePercent")),
                }
            )
        markets.sort(key=lambda item: item["quote_volume_24h"], reverse=True)
        markets = markets[:max_pairs]
        return {
            "connector_name": connector_name,
            "quote_asset": quote_asset,
            "trading_pairs": [item["trading_pair"] for item in markets],
            "markets": markets,
        }

    async def get_snapshot(
        self,
        connector_name: str,
        trading_pairs: List[str],
        interval: str = "1h",
        max_records: int = 72,
        order_book_depth: int = 100,
    ) -> Dict[str, Any]:
        prices = await self._market_data_service.get_prices(connector_name, trading_pairs)
        trading_rules = await self._market_data_service.get_trading_rules(connector_name, trading_pairs)
        market_items = await asyncio.gather(
            *(
                self._build_snapshot_item(
                    connector_name=connector_name,
                    trading_pair=pair,
                    interval=interval,
                    max_records=max_records,
                    order_book_depth=order_book_depth,
                    price=prices.get(pair) if isinstance(prices, dict) else None,
                    trading_rule=trading_rules.get(pair) if isinstance(trading_rules, dict) else None,
                )
                for pair in trading_pairs
            )
        )
        markets = {item["trading_pair"]: item for item in market_items}
        return {
            "created_at": time.time(),
            "connector_name": connector_name,
            "trading_pairs": trading_pairs,
            "markets": markets,
        }

    async def get_perp_pressure(self, connector_name: str, trading_pairs: List[str]) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            items = await asyncio.gather(
                *(self._get_pair_pressure(session, connector_name, pair) for pair in trading_pairs)
            )
        return {
            "created_at": time.time(),
            "connector_name": connector_name,
            "markets": {item["trading_pair"]: item for item in items},
        }

    async def get_candidates(
        self,
        connector_name: str,
        trading_pairs: List[str],
        interval: str = "1h",
        max_records: int = 72,
        order_book_depth: int = 100,
        min_score: int = 60,
    ) -> Dict[str, Any]:
        snapshot, pressure = await asyncio.gather(
            self.get_snapshot(connector_name, trading_pairs, interval, max_records, order_book_depth),
            self.get_perp_pressure(connector_name, trading_pairs),
        )
        candidates = []
        for pair, market in snapshot["markets"].items():
            feature = self._compute_feature(market, pressure["markets"].get(pair, {}))
            feature["tradable"] = feature["score"] >= min_score and not feature.get("snapshot_errors")
            candidates.append(feature)
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return {
            "created_at": time.time(),
            "connector_name": connector_name,
            "candidates": candidates,
        }

    async def _get_pair_pressure(self, session: aiohttp.ClientSession, connector_name: str, trading_pair: str) -> Dict[str, Any]:
        symbol = hb_pair_to_binance_symbol(trading_pair)
        errors = []
        try:
            funding_info = await self._market_data_service.get_funding_info(connector_name, trading_pair)
            if "error" in funding_info:
                errors.append(f"funding_info: {funding_info['error']}")
                funding_info = {}
        except Exception as exc:
            errors.append(f"funding_info: {exc}")
            funding_info = {}
        try:
            oi = await self._fetch_json(session, f"{BINANCE_FAPI_URL}/fapi/v1/openInterest", {"symbol": symbol})
        except Exception as exc:
            errors.append(f"open_interest: {exc}")
            oi = {}
        open_interest_history = await self._get_open_interest_history(session, symbol)
        mark_price = safe_float(funding_info.get("mark_price"))
        index_price = safe_float(funding_info.get("index_price"))
        funding_rate = safe_float(funding_info.get("funding_rate"))
        open_interest = safe_float(oi.get("openInterest"))
        basis_pct = None
        if mark_price is not None and index_price not in (None, 0):
            basis_pct = (mark_price - index_price) / index_price * 100
        return {
            "trading_pair": trading_pair,
            "symbol": symbol,
            "funding_rate": funding_rate,
            "funding_rate_pct": funding_rate * 100 if funding_rate is not None else None,
            "mark_price": mark_price,
            "index_price": index_price,
            "basis_pct": basis_pct,
            "open_interest": open_interest,
            "open_interest_history": open_interest_history,
            "open_interest_change_pct_5m": self._history_change_pct(open_interest, open_interest_history, 1),
            "open_interest_change_pct_1h": self._history_change_pct(open_interest, open_interest_history, 12),
            "open_interest_change_pct_4h": self._history_change_pct(open_interest, open_interest_history, 48),
            "errors": errors,
        }

    def _compute_feature(self, market: Dict[str, Any], pressure: Dict[str, Any]) -> Dict[str, Any]:
        order_book = market.get("order_book") or {}
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        best_bid, _ = self._extract_price_amount(bids[0]) if bids else (None, None)
        best_ask, _ = self._extract_price_amount(asks[0]) if asks else (None, None)
        mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else safe_float(market.get("price"))
        spread_bps = (
            (best_ask - best_bid) / mid * 10000
            if best_bid is not None and best_ask is not None and mid not in (None, 0)
            else None
        )
        candles = market.get("candles") or []
        closes = []
        for candle in candles:
            close = safe_float(candle.get("close"))
            if close is not None:
                closes.append(close)
        regime = "unstable"
        if len(closes) >= 24:
            change = (closes[-1] - closes[-24]) / closes[-24]
            if change > 0.04:
                regime = "uptrend"
            elif change < -0.04:
                regime = "downtrend"
            else:
                regime = "range"
        depth_20 = self._depth_quote(bids, asks, mid or 0, 20)
        depth_50 = self._depth_quote(bids, asks, mid or 0, 50)
        score = 100
        reasons = []
        alerts = []
        if spread_bps is None or spread_bps > 8:
            score -= 25
            alerts.append("wide_or_missing_spread")
        else:
            reasons.append("spread_ok")
        if depth_50 < 50000:
            score -= 25
            alerts.append("thin_depth_50bps")
        else:
            reasons.append("depth_ok")
        if abs(pressure.get("funding_rate_pct") or 0) > 0.03:
            score -= 10
            alerts.append("funding_extreme")
        if abs(pressure.get("basis_pct") or 0) > 0.2:
            score -= 10
            alerts.append("basis_extreme")
        if regime == "unstable":
            score -= 20
            alerts.append("unstable_regime")
        else:
            reasons.append(f"{regime}_regime")
        combined_errors = list(dict.fromkeys((market.get("errors") or []) + (pressure.get("errors") or [])))
        return {
            "trading_pair": market["trading_pair"],
            "score": max(score, 0),
            "market_regime": regime,
            "price": mid,
            "spread_bps": spread_bps,
            "order_book_depth_20bps_quote": depth_20,
            "order_book_depth_50bps_quote": depth_50,
            "funding_rate_pct": pressure.get("funding_rate_pct"),
            "basis_pct": pressure.get("basis_pct"),
            "open_interest": pressure.get("open_interest"),
            "open_interest_change_pct_5m": pressure.get("open_interest_change_pct_5m"),
            "open_interest_change_pct_1h": pressure.get("open_interest_change_pct_1h"),
            "open_interest_change_pct_4h": pressure.get("open_interest_change_pct_4h"),
            "open_interest_history": pressure.get("open_interest_history") or [],
            "trading_rule": market.get("trading_rule"),
            "snapshot_errors": combined_errors,
            "reason_codes": reasons,
            "market_alerts": alerts,
        }

    @staticmethod
    def _depth_quote(bids: List[Any], asks: List[Any], mid: float, bps: float) -> float:
        if mid <= 0:
            return 0
        bid_min = mid * (1 - bps / 10000)
        ask_max = mid * (1 + bps / 10000)
        bid_quote = sum(
            price * amount
            for price, amount in (USDCPerpMarketService._extract_price_amount(level) for level in bids)
            if price is not None and amount is not None and price >= bid_min
        )
        ask_quote = sum(
            price * amount
            for price, amount in (USDCPerpMarketService._extract_price_amount(level) for level in asks)
            if price is not None and amount is not None and price <= ask_max
        )
        return bid_quote + ask_quote

    async def _build_snapshot_item(
        self,
        connector_name: str,
        trading_pair: str,
        interval: str,
        max_records: int,
        order_book_depth: int,
        price: Optional[float],
        trading_rule: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        candles_task = asyncio.create_task(
            self._get_ready_candles(
                connector_name=connector_name,
                trading_pair=trading_pair,
                interval=interval,
                max_records=max_records,
            )
        )
        order_book_task = asyncio.create_task(
            self._get_ready_order_book(
                connector_name=connector_name,
                trading_pair=trading_pair,
                order_book_depth=order_book_depth,
            )
        )
        funding_task = asyncio.create_task(self._market_data_service.get_funding_info(connector_name, trading_pair))

        candles, candle_error = await candles_task
        order_book, order_book_error = await order_book_task
        funding_info = await funding_task

        errors = []
        if candle_error:
            errors.append(candle_error)
        if order_book_error:
            errors.append(order_book_error)
        if "error" in funding_info:
            errors.append(f"funding_info: {funding_info['error']}")
            funding_info = {}

        return {
            "trading_pair": trading_pair,
            "price": price,
            "candles": candles,
            "order_book": order_book,
            "funding_info": funding_info,
            "trading_rule": trading_rule,
            "errors": errors,
        }

    async def _get_ready_candles(
        self,
        connector_name: str,
        trading_pair: str,
        interval: str,
        max_records: int,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        try:
            feed = self._market_data_service.get_candles_feed(
                CandlesConfig(
                    connector=connector_name,
                    trading_pair=trading_pair,
                    interval=interval,
                    max_records=max_records,
                )
            )
        except Exception as exc:
            return [], f"candles: {exc}"

        deadline = time.monotonic() + CANDLES_READY_TIMEOUT
        last_records: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            if getattr(feed, "ready", False):
                candles_df = feed.candles_df
                if candles_df is not None and not candles_df.empty:
                    return candles_df.tail(max_records).to_dict(orient="records"), None
            candles_df = feed.candles_df
            if candles_df is not None and not candles_df.empty:
                last_records = candles_df.tail(max_records).to_dict(orient="records")
            await asyncio.sleep(CANDLES_READY_POLL_INTERVAL)

        if last_records:
            return last_records, "candles: feed not fully ready before timeout"
        return [], "candles: no candle data available before timeout"

    async def _get_ready_order_book(
        self,
        connector_name: str,
        trading_pair: str,
        order_book_depth: int,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        initialized = await self._market_data_service.initialize_order_book(
            connector_name=connector_name,
            trading_pair=trading_pair,
            timeout=ORDER_BOOK_READY_TIMEOUT,
        )
        if not initialized:
            return {}, "order_book: initialization timed out"

        order_book = await self._market_data_service.get_order_book_data(connector_name, trading_pair, order_book_depth)
        if "error" in order_book:
            return {}, f"order_book: {order_book['error']}"
        if not order_book.get("bids") or not order_book.get("asks"):
            return order_book, "order_book: empty bids or asks"
        return order_book, None

    async def _get_open_interest_history(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
    ) -> List[Dict[str, Any]]:
        try:
            items = await self._fetch_json(
                session,
                f"{BINANCE_FAPI_URL}/futures/data/openInterestHist",
                {"symbol": symbol, "period": "5m", "limit": OPEN_INTEREST_HISTORY_LIMIT},
            )
        except Exception:
            return []
        history = []
        for item in items or []:
            open_interest = safe_float(item.get("sumOpenInterest") or item.get("openInterest"))
            timestamp = safe_float(item.get("timestamp"))
            if open_interest is None or timestamp is None:
                continue
            history.append(
                {
                    "timestamp": timestamp,
                    "open_interest": open_interest,
                    "open_interest_value": safe_float(item.get("sumOpenInterestValue")),
                }
            )
        return history

    @staticmethod
    def _extract_price_amount(level: Any) -> Tuple[Optional[float], Optional[float]]:
        if isinstance(level, dict):
            return safe_float(level.get("price")), safe_float(level.get("amount"))
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            return safe_float(level[0]), safe_float(level[1])
        return None, None

    @staticmethod
    def _history_change_pct(current_value: Optional[float], history: List[Dict[str, Any]], periods_back: int) -> Optional[float]:
        if current_value in (None, 0) or len(history) < periods_back:
            return None
        previous = safe_float(history[-periods_back].get("open_interest"))
        if previous in (None, 0):
            return None
        return (current_value - previous) / previous * 100

    @staticmethod
    async def _fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        async with session.get(url, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"{url} failed with {response.status}: {text[:300]}")
            return json.loads(text)
