import asyncio
import json
import math
import statistics
import time
from typing import Any, Dict, List, Optional

import aiohttp

from services.market_data_service import MarketDataService


BINANCE_FAPI_URL = "https://fapi.binance.com"


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
        markets: Dict[str, Any] = {}
        for pair in trading_pairs:
            errors = []
            candles = []
            try:
                candles_df = self._market_data_service.get_candles_df(connector_name, pair, interval, max_records)
                candles = candles_df.tail(max_records).to_dict(orient="records") if candles_df is not None else []
            except Exception as exc:
                errors.append(f"candles: {exc}")
            order_book = await self._market_data_service.get_order_book_data(connector_name, pair, order_book_depth)
            if "error" in order_book:
                errors.append(f"order_book: {order_book['error']}")
                order_book = {}
            funding_info = await self._market_data_service.get_funding_info(connector_name, pair)
            if "error" in funding_info:
                errors.append(f"funding_info: {funding_info['error']}")
                funding_info = {}
            markets[pair] = {
                "trading_pair": pair,
                "price": prices.get(pair) if isinstance(prices, dict) else None,
                "candles": candles,
                "order_book": order_book,
                "funding_info": funding_info,
                "trading_rule": trading_rules.get(pair) if isinstance(trading_rules, dict) else None,
                "errors": errors,
            }
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
            feature["tradable"] = feature["score"] >= min_score and not market.get("errors")
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
        mark_price = safe_float(funding_info.get("mark_price"))
        index_price = safe_float(funding_info.get("index_price"))
        funding_rate = safe_float(funding_info.get("funding_rate"))
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
            "open_interest": safe_float(oi.get("openInterest")),
            "errors": errors,
        }

    def _compute_feature(self, market: Dict[str, Any], pressure: Dict[str, Any]) -> Dict[str, Any]:
        order_book = market.get("order_book") or {}
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        best_bid = safe_float(bids[0]["price"]) if bids else None
        best_ask = safe_float(asks[0]["price"]) if asks else None
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else safe_float(market.get("price"))
        spread_bps = (best_ask - best_bid) / mid * 10000 if best_bid and best_ask and mid else None
        candles = market.get("candles") or []
        closes = [safe_float(candle.get("close")) for candle in candles if safe_float(candle.get("close"))]
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
            "reason_codes": reasons,
            "market_alerts": alerts,
        }

    @staticmethod
    def _depth_quote(bids: List[Dict[str, Any]], asks: List[Dict[str, Any]], mid: float, bps: float) -> float:
        if mid <= 0:
            return 0
        bid_min = mid * (1 - bps / 10000)
        ask_max = mid * (1 + bps / 10000)
        bid_quote = sum((safe_float(x.get("price"), 0) or 0) * (safe_float(x.get("amount"), 0) or 0) for x in bids if (safe_float(x.get("price"), 0) or 0) >= bid_min)
        ask_quote = sum((safe_float(x.get("price"), 0) or 0) * (safe_float(x.get("amount"), 0) or 0) for x in asks if (safe_float(x.get("price"), 0) or 0) <= ask_max)
        return bid_quote + ask_quote

    @staticmethod
    async def _fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        async with session.get(url, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"{url} failed with {response.status}: {text[:300]}")
            return json.loads(text)

