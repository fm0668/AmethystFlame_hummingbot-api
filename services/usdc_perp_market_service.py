import asyncio
import json
import math
import statistics
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
FUNDING_HISTORY_LIMIT = 30
KLINE_INTERVAL_LIMITS = {"5m": 96, "15m": 96, "1h": 72}
DEFAULT_FEE_RATE = 0.0004
DEFAULT_SLIPPAGE_QUERY_QUOTE = 1000.0


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

    async def get_decision_candidates(
        self,
        connector_name: str = "binance_perpetual",
        quote_asset: str = "USDC",
        universe_max_pairs: int = 32,
        top_n: int = 10,
        interval: str = "1h",
        max_records: int = 72,
        order_book_depth: int = 100,
        min_score: int = 60,
        min_24h_quote_volume: float = 0,
    ) -> Dict[str, Any]:
        universe = await self.get_universe(
            connector_name=connector_name,
            quote_asset=quote_asset,
            max_pairs=universe_max_pairs,
            min_24h_quote_volume=min_24h_quote_volume,
        )
        universe_markets = {item["trading_pair"]: item for item in universe.get("markets", [])}
        trading_pairs = universe.get("trading_pairs", [])
        if not trading_pairs:
            return {
                "generated_at": time.time(),
                "connector_name": connector_name,
                "universe_size": 0,
                "selected_count": 0,
                "watch_pool": [],
                "excluded_pairs": [],
                "screening_candidates": [],
                "decision_candidates": [],
            }

        snapshot, pressure = await asyncio.gather(
            self.get_snapshot(
                connector_name=connector_name,
                trading_pairs=trading_pairs,
                interval=interval,
                max_records=max_records,
                order_book_depth=order_book_depth,
            ),
            self.get_perp_pressure(connector_name=connector_name, trading_pairs=trading_pairs),
        )

        async with aiohttp.ClientSession() as session:
            candles_15m, candles_1h = await asyncio.gather(
                self._fetch_kline_batch(session, trading_pairs, "15m", KLINE_INTERVAL_LIMITS["15m"]),
                self._fetch_kline_batch(session, trading_pairs, "1h", KLINE_INTERVAL_LIMITS["1h"]),
            )

        screening_candidates: List[Dict[str, Any]] = []
        watch_pool_entries: List[Dict[str, Any]] = []
        excluded_pairs: List[Dict[str, Any]] = []

        for trading_pair in trading_pairs:
            market = snapshot["markets"].get(trading_pair, {})
            pair_pressure = pressure["markets"].get(trading_pair, {})
            candidate = self._compute_feature(market, pair_pressure)
            screening = self._build_screening_candidate(
                universe_market=universe_markets.get(trading_pair, {}),
                candidate=candidate,
                market=market,
                pressure=pair_pressure,
                candles_15m=candles_15m.get(trading_pair, []),
                candles_1h=candles_1h.get(trading_pair, []),
                min_score=min_score,
            )
            screening_candidates.append(screening)
            if screening["excluded"]:
                excluded_pairs.append(
                    {
                        "trading_pair": trading_pair,
                        "reason_codes": screening["exclusion_reasons"],
                        "market_alerts": screening.get("market_alerts", []),
                        "data_quality_score": screening.get("data_quality_score"),
                    }
                )
            else:
                watch_pool_entries.append(screening)

        screening_candidates.sort(
            key=lambda item: (
                item.get("excluded", False),
                -(item.get("screening_score") or 0),
                -(item.get("quote_volume_24h") or 0),
            )
        )
        watch_pool_entries.sort(
            key=lambda item: (
                -(item.get("screening_score") or 0),
                -(item.get("data_quality_score") or 0),
                -(item.get("quote_volume_24h") or 0),
            )
        )
        watch_pool = [item["trading_pair"] for item in watch_pool_entries]
        selected_pairs = watch_pool[:top_n]

        decision_candidates: List[Dict[str, Any]] = []
        if selected_pairs:
            async with aiohttp.ClientSession() as session:
                candles_5m, funding_history = await asyncio.gather(
                    self._fetch_kline_batch(session, selected_pairs, "5m", KLINE_INTERVAL_LIMITS["5m"]),
                    self._fetch_funding_rate_batch(session, selected_pairs, FUNDING_HISTORY_LIMIT),
                )
            screening_lookup = {item["trading_pair"]: item for item in screening_candidates}
            for trading_pair in selected_pairs:
                decision_candidates.append(
                    self._build_decision_candidate(
                        screening=screening_lookup[trading_pair],
                        market=snapshot["markets"].get(trading_pair, {}),
                        pressure=pressure["markets"].get(trading_pair, {}),
                        candles_5m=candles_5m.get(trading_pair, []),
                        candles_15m=candles_15m.get(trading_pair, []),
                        candles_1h=candles_1h.get(trading_pair, []),
                        funding_history=funding_history.get(trading_pair, []),
                    )
                )

        return {
            "generated_at": time.time(),
            "connector_name": connector_name,
            "universe_size": len(trading_pairs),
            "selected_count": len(decision_candidates),
            "watch_pool": watch_pool,
            "excluded_pairs": excluded_pairs,
            "screening_candidates": screening_candidates,
            "decision_candidates": decision_candidates,
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

    def _build_screening_candidate(
        self,
        universe_market: Dict[str, Any],
        candidate: Dict[str, Any],
        market: Dict[str, Any],
        pressure: Dict[str, Any],
        candles_15m: List[Dict[str, Any]],
        candles_1h: List[Dict[str, Any]],
        min_score: int,
    ) -> Dict[str, Any]:
        closes_15m = self._candle_series(candles_15m, "close")
        closes_1h = self._candle_series(candles_1h, "close")
        quote_volumes_1h = self._candle_series(candles_1h, "quote_volume")
        trade_counts_1h = self._candle_series(candles_1h, "trade_count")

        quote_volume_1h = quote_volumes_1h[-1] if quote_volumes_1h else None
        quote_volume_1h_zscore = self._zscore(quote_volumes_1h)
        trade_count_1h = trade_counts_1h[-1] if trade_counts_1h else None
        atr_15m_pct = self._atr_pct(candles_15m, 14)
        atr_1h_pct = self._atr_pct(candles_1h, 14)
        realized_vol_1h = self._realized_vol(closes_15m, 4)
        realized_vol_24h = self._realized_vol(closes_1h, 24)
        range_width_24h_pct = self._range_width_pct(candles_1h, 24)
        price_change_15m_pct = self._pct_change_last(closes_15m, 1)
        price_change_1h_pct = self._pct_change_last(closes_1h, 1)
        price_change_24h_pct = self._pct_change_last(closes_1h, 24)
        data_quality_score = self._data_quality_score(
            market_errors=market.get("errors") or [],
            pressure_errors=pressure.get("errors") or [],
            trading_rule=market.get("trading_rule"),
            spread_bps=candidate.get("spread_bps"),
            depth_50=candidate.get("order_book_depth_50bps_quote"),
            candles_15m=candles_15m,
            candles_1h=candles_1h,
        )
        screening_score = self._screening_score(
            candidate_score=candidate.get("score"),
            data_quality_score=data_quality_score,
            quote_volume_24h=universe_market.get("quote_volume_24h"),
            quote_volume_1h_zscore=quote_volume_1h_zscore,
        )
        exclusion_reasons = self._screening_exclusion_reasons(
            candidate=candidate,
            universe_market=universe_market,
            data_quality_score=data_quality_score,
            min_score=min_score,
        )

        return {
            "trading_pair": candidate["trading_pair"],
            "symbol": hb_pair_to_binance_symbol(candidate["trading_pair"]),
            "last_price": candidate.get("price") or universe_market.get("last_price"),
            "quote_volume_24h": universe_market.get("quote_volume_24h"),
            "quote_volume_1h": quote_volume_1h,
            "quote_volume_1h_zscore": quote_volume_1h_zscore,
            "trade_count_1h": trade_count_1h,
            "spread_bps": candidate.get("spread_bps"),
            "order_book_depth_20bps_quote": candidate.get("order_book_depth_20bps_quote"),
            "order_book_depth_50bps_quote": candidate.get("order_book_depth_50bps_quote"),
            "atr_15m_pct": atr_15m_pct,
            "atr_1h_pct": atr_1h_pct,
            "realized_vol_1h": realized_vol_1h,
            "realized_vol_24h": realized_vol_24h,
            "range_width_24h_pct": range_width_24h_pct,
            "price_change_15m_pct": price_change_15m_pct,
            "price_change_1h_pct": price_change_1h_pct,
            "price_change_24h_pct": price_change_24h_pct,
            "funding_rate_pct": candidate.get("funding_rate_pct"),
            "mark_index_basis_pct": candidate.get("basis_pct"),
            "open_interest_change_1h_pct": candidate.get("open_interest_change_pct_1h"),
            "data_quality_score": data_quality_score,
            "screening_score": screening_score,
            "market_regime": candidate.get("market_regime"),
            "tradable": candidate.get("score", 0) >= min_score and not candidate.get("snapshot_errors"),
            "score": candidate.get("score"),
            "reason_codes": candidate.get("reason_codes") or [],
            "market_alerts": candidate.get("market_alerts") or [],
            "snapshot_errors": candidate.get("snapshot_errors") or [],
            "trading_rule": candidate.get("trading_rule"),
            "excluded": bool(exclusion_reasons),
            "exclusion_reasons": exclusion_reasons,
        }

    def _build_decision_candidate(
        self,
        screening: Dict[str, Any],
        market: Dict[str, Any],
        pressure: Dict[str, Any],
        candles_5m: List[Dict[str, Any]],
        candles_15m: List[Dict[str, Any]],
        candles_1h: List[Dict[str, Any]],
        funding_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        order_book = market.get("order_book") or {}
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        mid_price = screening.get("last_price") or screening.get("price")
        top_1_depth_quote = self._top_level_depth_quote(bids, asks)
        depth_100bps_quote = self._depth_quote(bids, asks, mid_price or 0, 100)
        slippage_buy_1000_quote_bps = self._slippage_for_quote_volume_bps(asks, mid_price, DEFAULT_SLIPPAGE_QUERY_QUOTE)
        slippage_sell_1000_quote_bps = self._slippage_for_quote_volume_bps(bids, mid_price, DEFAULT_SLIPPAGE_QUERY_QUOTE)

        atr_5m_pct = self._atr_pct(candles_5m, 14)
        range_width_6h_pct = self._range_width_pct(candles_5m, 72)
        range_touch_count = self._range_touch_count(candles_1h, 24)
        range_mid_reversion_score = self._range_mid_reversion_score(candles_1h, 24)
        upper_wick_pressure = self._wick_pressure(candles_15m, "upper", 16)
        lower_wick_pressure = self._wick_pressure(candles_15m, "lower", 16)

        ema_slope_15m = self._ema_slope_pct(candles_15m, 16, 4)
        ema_slope_1h = self._ema_slope_pct(candles_1h, 12, 4)
        trend_strength_score = self._trend_strength_score(candles_15m, candles_1h)
        support_distance_pct, resistance_distance_pct = self._support_resistance_distances(candles_1h, 24)
        breakout_distance_pct = min(
            value
            for value in [support_distance_pct, resistance_distance_pct]
            if value is not None
        ) if support_distance_pct is not None or resistance_distance_pct is not None else None
        trend_consistency = self._trend_consistency(candles_5m, candles_15m, candles_1h)

        volume_15m_zscore = self._zscore(self._candle_series(candles_15m, "quote_volume"))
        volume_1h_zscore = screening.get("quote_volume_1h_zscore")
        volume_24h_percentile = self._percentile_rank(self._candle_series(candles_1h, "quote_volume"))
        volume_price_confirmation = self._volume_price_confirmation(volume_1h_zscore, screening.get("price_change_1h_pct"))
        abnormal_volume_direction = self._abnormal_volume_direction(volume_1h_zscore, screening.get("price_change_1h_pct"))

        open_interest_history = pressure.get("open_interest_history") or []
        open_interest_change_15m_pct = self._history_change_pct(pressure.get("open_interest"), open_interest_history, 3)
        funding_rate_zscore = self._funding_rate_zscore(funding_history, pressure.get("funding_rate_pct"))
        oi_price_divergence = self._oi_price_divergence(
            pressure.get("open_interest_change_pct_1h"),
            screening.get("price_change_1h_pct"),
        )
        long_crowding_score = self._crowding_score(
            direction="long",
            funding_rate_pct=pressure.get("funding_rate_pct"),
            basis_pct=pressure.get("basis_pct"),
            open_interest_change_pct=pressure.get("open_interest_change_pct_1h"),
            price_change_pct=screening.get("price_change_1h_pct"),
        )
        short_crowding_score = self._crowding_score(
            direction="short",
            funding_rate_pct=pressure.get("funding_rate_pct"),
            basis_pct=pressure.get("basis_pct"),
            open_interest_change_pct=pressure.get("open_interest_change_pct_1h"),
            price_change_pct=screening.get("price_change_1h_pct"),
        )
        liquidation_risk_proxy = self._liquidation_risk_proxy(
            long_crowding_score=long_crowding_score,
            short_crowding_score=short_crowding_score,
            trend_strength_score=trend_strength_score,
            atr_15m_pct=screening.get("atr_15m_pct"),
        )

        trading_rule = market.get("trading_rule") or {}
        min_notional = safe_float(trading_rule.get("min_notional_size") or trading_rule.get("min_order_value"), 0) or 0
        estimated_order_amount_quote = self._estimated_order_amount_quote(
            min_notional=min_notional,
            depth_20bps_quote=screening.get("order_book_depth_20bps_quote"),
            depth_50bps_quote=screening.get("order_book_depth_50bps_quote"),
        )
        order_size_to_depth_ratio = (
            estimated_order_amount_quote / screening["order_book_depth_50bps_quote"]
            if screening.get("order_book_depth_50bps_quote")
            else None
        )
        fee_rate = DEFAULT_FEE_RATE
        min_spread_required_bps = (
            fee_rate * 2 * 10000
            + (slippage_buy_1000_quote_bps or 0)
            + (slippage_sell_1000_quote_bps or 0)
        )
        grid_step_pct = max(
            min_spread_required_bps / 100 * 1.25,
            (atr_5m_pct or screening.get("atr_15m_pct") or 0.2) * 0.35,
            0.2,
        )
        max_grid_levels = self._max_grid_levels(screening.get("range_width_24h_pct"), grid_step_pct)
        expected_fee_per_cycle = estimated_order_amount_quote * fee_rate * 2

        recommended_grid_modes = self._recommended_grid_modes(
            market_regime=screening.get("market_regime"),
            long_crowding_score=long_crowding_score,
            short_crowding_score=short_crowding_score,
            support_distance_pct=support_distance_pct,
            resistance_distance_pct=resistance_distance_pct,
        )
        risk_flags = self._build_risk_flags(
            screening=screening,
            long_crowding_score=long_crowding_score,
            short_crowding_score=short_crowding_score,
            liquidation_risk_proxy=liquidation_risk_proxy,
            volume_1h_zscore=volume_1h_zscore,
            breakout_distance_pct=breakout_distance_pct,
        )

        return {
            "symbol": screening["trading_pair"],
            "market_regime": screening.get("market_regime"),
            "recommended_grid_modes": recommended_grid_modes,
            "screening": screening,
            "liquidity": {
                "spread_bps": screening.get("spread_bps"),
                "top_1_depth_quote": top_1_depth_quote,
                "depth_20bps_quote": screening.get("order_book_depth_20bps_quote"),
                "depth_50bps_quote": screening.get("order_book_depth_50bps_quote"),
                "depth_100bps_quote": depth_100bps_quote,
                "slippage_buy_1000_quote_bps": slippage_buy_1000_quote_bps,
                "slippage_sell_1000_quote_bps": slippage_sell_1000_quote_bps,
                "order_size_to_depth_ratio": order_size_to_depth_ratio,
            },
            "volatility": {
                "atr_5m_pct": atr_5m_pct,
                "atr_15m_pct": screening.get("atr_15m_pct"),
                "atr_1h_pct": screening.get("atr_1h_pct"),
                "realized_vol_1h": screening.get("realized_vol_1h"),
                "realized_vol_24h": screening.get("realized_vol_24h"),
                "range_width_6h_pct": range_width_6h_pct,
                "range_width_24h_pct": screening.get("range_width_24h_pct"),
                "range_touch_count": range_touch_count,
                "range_mid_reversion_score": range_mid_reversion_score,
                "upper_wick_pressure": upper_wick_pressure,
                "lower_wick_pressure": lower_wick_pressure,
            },
            "trend": {
                "ema_slope_15m": ema_slope_15m,
                "ema_slope_1h": ema_slope_1h,
                "trend_strength_score": trend_strength_score,
                "breakout_distance_pct": breakout_distance_pct,
                "support_distance_pct": support_distance_pct,
                "resistance_distance_pct": resistance_distance_pct,
                "trend_consistency_5m_15m_1h": trend_consistency,
            },
            "volume_profile": {
                "volume_15m_zscore": volume_15m_zscore,
                "volume_1h_zscore": volume_1h_zscore,
                "volume_24h_percentile": volume_24h_percentile,
                "volume_price_confirmation": volume_price_confirmation,
                "abnormal_volume_direction": abnormal_volume_direction,
            },
            "perp_pressure": {
                "funding_rate_pct": pressure.get("funding_rate_pct"),
                "funding_rate_zscore": funding_rate_zscore,
                "mark_index_basis_pct": pressure.get("basis_pct"),
                "open_interest": pressure.get("open_interest"),
                "open_interest_change_15m_pct": open_interest_change_15m_pct,
                "open_interest_change_1h_pct": pressure.get("open_interest_change_pct_1h"),
                "open_interest_change_4h_pct": pressure.get("open_interest_change_pct_4h"),
                "oi_price_divergence": oi_price_divergence,
                "long_crowding_score": long_crowding_score,
                "short_crowding_score": short_crowding_score,
                "liquidation_risk_proxy": liquidation_risk_proxy,
            },
            "execution": {
                "min_order_size": trading_rule.get("min_order_size"),
                "min_notional": min_notional,
                "price_tick_size": trading_rule.get("min_price_increment"),
                "amount_step_size": trading_rule.get("min_base_amount_increment"),
                "min_spread_required_bps": min_spread_required_bps,
                "grid_step_pct": grid_step_pct,
                "estimated_order_amount_quote": estimated_order_amount_quote,
                "max_grid_levels": max_grid_levels,
                "fee_rate": fee_rate,
                "expected_fee_per_cycle": expected_fee_per_cycle,
            },
            "reason_codes": list(
                dict.fromkeys(
                    (screening.get("reason_codes") or [])
                    + ([f"recommended_{recommended_grid_modes[0]}"] if recommended_grid_modes else [])
                )
            ),
            "risk_flags": risk_flags,
            "market_alerts": screening.get("market_alerts") or [],
            "snapshot_errors": screening.get("snapshot_errors") or [],
            "data_quality_score": screening.get("data_quality_score"),
            "trading_rule": trading_rule,
        }

    async def _fetch_kline_batch(
        self,
        session: aiohttp.ClientSession,
        trading_pairs: List[str],
        interval: str,
        limit: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        items = await asyncio.gather(
            *(self._fetch_binance_klines(session, hb_pair_to_binance_symbol(pair), interval, limit) for pair in trading_pairs)
        )
        return {pair: item for pair, item in zip(trading_pairs, items)}

    async def _fetch_funding_rate_batch(
        self,
        session: aiohttp.ClientSession,
        trading_pairs: List[str],
        limit: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        items = await asyncio.gather(
            *(self._fetch_funding_rate_history(session, hb_pair_to_binance_symbol(pair), limit) for pair in trading_pairs)
        )
        return {pair: item for pair, item in zip(trading_pairs, items)}

    def _screening_exclusion_reasons(
        self,
        candidate: Dict[str, Any],
        universe_market: Dict[str, Any],
        data_quality_score: float,
        min_score: int,
    ) -> List[str]:
        reasons = []
        if candidate.get("snapshot_errors"):
            reasons.append("snapshot_errors")
        if candidate.get("spread_bps") is None or candidate.get("spread_bps", 0) > 8:
            reasons.append("wide_spread")
        if (candidate.get("order_book_depth_50bps_quote") or 0) < 50000:
            reasons.append("thin_depth")
        if (universe_market.get("quote_volume_24h") or 0) <= 0:
            reasons.append("missing_quote_volume")
        if (candidate.get("score") or 0) < min_score:
            reasons.append("low_candidate_score")
        if data_quality_score < 0.7:
            reasons.append("low_data_quality")
        if not candidate.get("trading_rule"):
            reasons.append("missing_trading_rule")
        return reasons

    @staticmethod
    def _screening_score(
        candidate_score: Optional[float],
        data_quality_score: float,
        quote_volume_24h: Optional[float],
        quote_volume_1h_zscore: Optional[float],
    ) -> float:
        base_score = candidate_score or 0
        liquidity_bonus = min(math.log10((quote_volume_24h or 1) + 1) * 2, 10)
        anomaly_bonus = max(min((quote_volume_1h_zscore or 0) * 2, 8), -8)
        quality_bonus = data_quality_score * 12
        return round(base_score + liquidity_bonus + anomaly_bonus + quality_bonus, 4)

    @staticmethod
    def _data_quality_score(
        market_errors: List[str],
        pressure_errors: List[str],
        trading_rule: Optional[Dict[str, Any]],
        spread_bps: Optional[float],
        depth_50: Optional[float],
        candles_15m: List[Dict[str, Any]],
        candles_1h: List[Dict[str, Any]],
    ) -> float:
        score = 1.0
        if market_errors:
            score -= min(0.3, 0.1 * len(market_errors))
        if pressure_errors:
            score -= min(0.2, 0.1 * len(pressure_errors))
        if not trading_rule:
            score -= 0.15
        if spread_bps is None:
            score -= 0.15
        elif spread_bps > 8:
            score -= 0.1
        if (depth_50 or 0) < 50000:
            score -= 0.1
        if len(candles_15m) < 20:
            score -= 0.1
        if len(candles_1h) < 24:
            score -= 0.1
        return round(max(0.0, min(score, 1.0)), 4)

    @staticmethod
    def _candle_series(candles: List[Dict[str, Any]], key: str) -> List[float]:
        values = []
        for candle in candles:
            value = safe_float(candle.get(key))
            if value is not None:
                values.append(value)
        return values

    @staticmethod
    def _pct_change_last(values: List[float], periods_back: int) -> Optional[float]:
        if len(values) <= periods_back or values[-periods_back - 1] == 0:
            return None
        return (values[-1] - values[-periods_back - 1]) / values[-periods_back - 1] * 100

    @staticmethod
    def _atr_pct(candles: List[Dict[str, Any]], period: int) -> Optional[float]:
        if len(candles) < period + 1:
            return None
        trs = []
        previous_close = safe_float(candles[-period - 1].get("close"))
        for candle in candles[-period:]:
            high = safe_float(candle.get("high"))
            low = safe_float(candle.get("low"))
            close = safe_float(candle.get("close"))
            if high is None or low is None or close is None:
                continue
            if previous_close is None:
                tr = high - low
            else:
                tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
            trs.append(tr)
            previous_close = close
        current_close = safe_float(candles[-1].get("close"))
        if not trs or current_close in (None, 0):
            return None
        return sum(trs) / len(trs) / current_close * 100

    @staticmethod
    def _realized_vol(closes: List[float], window: int) -> Optional[float]:
        if len(closes) <= window:
            return None
        returns = []
        for index in range(len(closes) - window, len(closes)):
            current = closes[index]
            previous = closes[index - 1]
            if current <= 0 or previous <= 0:
                continue
            returns.append(math.log(current / previous))
        if len(returns) < 2:
            return None
        return statistics.pstdev(returns) * math.sqrt(len(returns))

    @staticmethod
    def _range_width_pct(candles: List[Dict[str, Any]], window: int) -> Optional[float]:
        if not candles:
            return None
        subset = candles[-window:]
        highs = [safe_float(item.get("high")) for item in subset]
        lows = [safe_float(item.get("low")) for item in subset]
        highs = [item for item in highs if item is not None]
        lows = [item for item in lows if item is not None]
        close = safe_float(subset[-1].get("close"))
        if not highs or not lows or close in (None, 0):
            return None
        return (max(highs) - min(lows)) / close * 100

    @staticmethod
    def _zscore(values: List[float]) -> Optional[float]:
        if len(values) < 5:
            return None
        history = values[:-1]
        latest = values[-1]
        if len(history) < 2:
            return None
        deviation = statistics.pstdev(history)
        if deviation == 0:
            return 0.0
        return (latest - statistics.mean(history)) / deviation

    @staticmethod
    def _ema(values: List[float], span: int) -> List[float]:
        if not values:
            return []
        multiplier = 2 / (span + 1)
        ema_values = [values[0]]
        for value in values[1:]:
            ema_values.append((value - ema_values[-1]) * multiplier + ema_values[-1])
        return ema_values

    def _ema_slope_pct(self, candles: List[Dict[str, Any]], span: int, compare_periods: int) -> Optional[float]:
        closes = self._candle_series(candles, "close")
        ema_values = self._ema(closes, span)
        if len(ema_values) <= compare_periods:
            return None
        base = ema_values[-compare_periods - 1]
        if base == 0:
            return None
        return (ema_values[-1] - base) / base * 100

    def _trend_strength_score(self, candles_15m: List[Dict[str, Any]], candles_1h: List[Dict[str, Any]]) -> Optional[float]:
        slope_15m = self._ema_slope_pct(candles_15m, 16, 4) or 0
        slope_1h = self._ema_slope_pct(candles_1h, 12, 4) or 0
        return round(min(abs(slope_15m) * 8 + abs(slope_1h) * 12, 100), 4)

    @staticmethod
    def _support_resistance_distances(candles: List[Dict[str, Any]], window: int) -> Tuple[Optional[float], Optional[float]]:
        if not candles:
            return None, None
        subset = candles[-window:]
        close = safe_float(subset[-1].get("close"))
        highs = [safe_float(item.get("high")) for item in subset]
        lows = [safe_float(item.get("low")) for item in subset]
        highs = [item for item in highs if item is not None]
        lows = [item for item in lows if item is not None]
        if close in (None, 0) or not highs or not lows:
            return None, None
        support = (close - min(lows)) / close * 100
        resistance = (max(highs) - close) / close * 100
        return support, resistance

    def _trend_consistency(
        self,
        candles_5m: List[Dict[str, Any]],
        candles_15m: List[Dict[str, Any]],
        candles_1h: List[Dict[str, Any]],
    ) -> Optional[float]:
        slopes = [
            self._ema_slope_pct(candles_5m, 24, 6),
            self._ema_slope_pct(candles_15m, 16, 4),
            self._ema_slope_pct(candles_1h, 12, 4),
        ]
        valid = [slope for slope in slopes if slope is not None]
        if not valid:
            return None
        positives = sum(1 for slope in valid if slope > 0)
        negatives = sum(1 for slope in valid if slope < 0)
        return max(positives, negatives) / len(valid)

    @staticmethod
    def _range_touch_count(candles: List[Dict[str, Any]], window: int) -> int:
        if not candles:
            return 0
        subset = candles[-window:]
        highs = [safe_float(item.get("high")) for item in subset]
        lows = [safe_float(item.get("low")) for item in subset]
        highs = [item for item in highs if item is not None]
        lows = [item for item in lows if item is not None]
        if not highs or not lows:
            return 0
        highest = max(highs)
        lowest = min(lows)
        tolerance = max((highest - lowest) * 0.1, 1e-9)
        touches = 0
        for candle in subset:
            high = safe_float(candle.get("high"))
            low = safe_float(candle.get("low"))
            if high is not None and highest - high <= tolerance:
                touches += 1
            if low is not None and low - lowest <= tolerance:
                touches += 1
        return touches

    @staticmethod
    def _range_mid_reversion_score(candles: List[Dict[str, Any]], window: int) -> Optional[float]:
        if not candles:
            return None
        subset = candles[-window:]
        highs = [safe_float(item.get("high")) for item in subset]
        lows = [safe_float(item.get("low")) for item in subset]
        closes = [safe_float(item.get("close")) for item in subset]
        highs = [item for item in highs if item is not None]
        lows = [item for item in lows if item is not None]
        closes = [item for item in closes if item is not None]
        if not highs or not lows or not closes:
            return None
        highest = max(highs)
        lowest = min(lows)
        if highest == lowest:
            return 1.0
        midpoint = (highest + lowest) / 2
        deviations = [abs(close - midpoint) / (highest - lowest) for close in closes]
        return max(0.0, 1 - statistics.mean(deviations) * 2)

    @staticmethod
    def _wick_pressure(candles: List[Dict[str, Any]], side: str, window: int) -> Optional[float]:
        subset = candles[-window:]
        ratios = []
        for candle in subset:
            high = safe_float(candle.get("high"))
            low = safe_float(candle.get("low"))
            open_price = safe_float(candle.get("open"))
            close = safe_float(candle.get("close"))
            if None in (high, low, open_price, close) or high == low:
                continue
            body_high = max(open_price, close)
            body_low = min(open_price, close)
            wick = high - body_high if side == "upper" else body_low - low
            ratios.append(max(wick, 0) / (high - low))
        if not ratios:
            return None
        return statistics.mean(ratios)

    @staticmethod
    def _percentile_rank(values: List[float]) -> Optional[float]:
        if len(values) < 2:
            return None
        latest = values[-1]
        history = values[:-1]
        less_or_equal = sum(1 for item in history if item <= latest)
        return less_or_equal / len(history)

    @staticmethod
    def _volume_price_confirmation(volume_zscore: Optional[float], price_change_pct: Optional[float]) -> str:
        if volume_zscore is None or price_change_pct is None or volume_zscore < 1:
            return "neutral"
        if price_change_pct > 0:
            return "bullish_confirmation"
        if price_change_pct < 0:
            return "bearish_confirmation"
        return "neutral"

    @staticmethod
    def _abnormal_volume_direction(volume_zscore: Optional[float], price_change_pct: Optional[float]) -> str:
        if volume_zscore is None or volume_zscore < 1 or price_change_pct is None:
            return "neutral"
        return "bullish" if price_change_pct > 0 else "bearish"

    @staticmethod
    def _funding_rate_zscore(funding_history: List[Dict[str, Any]], current_funding_rate_pct: Optional[float]) -> Optional[float]:
        if current_funding_rate_pct is None:
            return None
        history = [
            safe_float(item.get("funding_rate_pct"))
            for item in funding_history
            if safe_float(item.get("funding_rate_pct")) is not None
        ]
        if len(history) < 5:
            return None
        deviation = statistics.pstdev(history)
        if deviation == 0:
            return 0.0
        return (current_funding_rate_pct - statistics.mean(history)) / deviation

    @staticmethod
    def _oi_price_divergence(open_interest_change_pct: Optional[float], price_change_pct: Optional[float]) -> Optional[float]:
        if open_interest_change_pct is None or price_change_pct is None:
            return None
        return open_interest_change_pct - price_change_pct

    @staticmethod
    def _crowding_score(
        direction: str,
        funding_rate_pct: Optional[float],
        basis_pct: Optional[float],
        open_interest_change_pct: Optional[float],
        price_change_pct: Optional[float],
    ) -> float:
        funding = funding_rate_pct or 0
        basis = basis_pct or 0
        open_interest = open_interest_change_pct or 0
        price_change = price_change_pct or 0
        if direction == "long":
            components = [
                max(funding / 0.03, 0),
                max(basis / 0.2, 0),
                max(open_interest / 6, 0),
                max(price_change / 3, 0),
            ]
        else:
            components = [
                max(-funding / 0.03, 0),
                max(-basis / 0.2, 0),
                max(open_interest / 6, 0),
                max(-price_change / 3, 0),
            ]
        return round(min(statistics.mean(components), 1.0), 4)

    @staticmethod
    def _liquidation_risk_proxy(
        long_crowding_score: float,
        short_crowding_score: float,
        trend_strength_score: Optional[float],
        atr_15m_pct: Optional[float],
    ) -> float:
        strength = (trend_strength_score or 0) / 100
        volatility = min((atr_15m_pct or 0) / 2, 1.0)
        return round(min(max(long_crowding_score, short_crowding_score) * (1 + strength + volatility), 1.0), 4)

    @staticmethod
    def _top_level_depth_quote(bids: List[Any], asks: List[Any]) -> float:
        bid_price, bid_amount = USDCPerpMarketService._extract_price_amount(bids[0]) if bids else (None, None)
        ask_price, ask_amount = USDCPerpMarketService._extract_price_amount(asks[0]) if asks else (None, None)
        total = 0.0
        if bid_price is not None and bid_amount is not None:
            total += bid_price * bid_amount
        if ask_price is not None and ask_amount is not None:
            total += ask_price * ask_amount
        return total

    @staticmethod
    def _slippage_for_quote_volume_bps(levels: List[Any], mid_price: Optional[float], quote_volume: float) -> Optional[float]:
        if mid_price in (None, 0):
            return None
        average_price = USDCPerpMarketService._average_price_for_quote_volume(levels, quote_volume)
        if average_price is None:
            return None
        return abs(average_price - mid_price) / mid_price * 10000

    @staticmethod
    def _average_price_for_quote_volume(levels: List[Any], quote_volume: float) -> Optional[float]:
        if quote_volume <= 0:
            return None
        remaining_quote = quote_volume
        acquired_base = 0.0
        spent_quote = 0.0
        for level in levels:
            price, amount = USDCPerpMarketService._extract_price_amount(level)
            if price in (None, 0) or amount in (None, 0):
                continue
            level_quote = price * amount
            take_quote = min(level_quote, remaining_quote)
            acquired_base += take_quote / price
            spent_quote += take_quote
            remaining_quote -= take_quote
            if remaining_quote <= 1e-9:
                break
        if spent_quote <= 0 or acquired_base <= 0:
            return None
        return spent_quote / acquired_base

    @staticmethod
    def _estimated_order_amount_quote(
        min_notional: float,
        depth_20bps_quote: Optional[float],
        depth_50bps_quote: Optional[float],
    ) -> float:
        safe_minimum = max(min_notional * 1.2, 25.0)
        liquidity_budget = max(
            safe_minimum,
            min((depth_20bps_quote or safe_minimum) * 0.01, (depth_50bps_quote or safe_minimum) * 0.005, 500.0),
        )
        return round(liquidity_budget, 4)

    @staticmethod
    def _max_grid_levels(range_width_pct: Optional[float], grid_step_pct: Optional[float]) -> int:
        if range_width_pct in (None, 0) or grid_step_pct in (None, 0):
            return 3
        return max(3, min(int(range_width_pct / grid_step_pct), 20))

    @staticmethod
    def _recommended_grid_modes(
        market_regime: Optional[str],
        long_crowding_score: float,
        short_crowding_score: float,
        support_distance_pct: Optional[float],
        resistance_distance_pct: Optional[float],
    ) -> List[str]:
        if market_regime == "uptrend" and long_crowding_score < 0.8:
            return ["long_grid", "neutral_grid"]
        if market_regime == "downtrend" and short_crowding_score < 0.8:
            return ["short_grid", "neutral_grid"]
        if market_regime == "range":
            if support_distance_pct is not None and resistance_distance_pct is not None:
                if support_distance_pct < resistance_distance_pct and short_crowding_score > 0.75:
                    return ["long_grid", "neutral_grid"]
                if resistance_distance_pct < support_distance_pct and long_crowding_score > 0.75:
                    return ["short_grid", "neutral_grid"]
            return ["neutral_grid", "long_grid", "short_grid"]
        return ["neutral_grid"]

    @staticmethod
    def _build_risk_flags(
        screening: Dict[str, Any],
        long_crowding_score: float,
        short_crowding_score: float,
        liquidation_risk_proxy: float,
        volume_1h_zscore: Optional[float],
        breakout_distance_pct: Optional[float],
    ) -> List[str]:
        risk_flags = list(screening.get("market_alerts") or [])
        if volume_1h_zscore is not None and volume_1h_zscore > 2:
            risk_flags.append("volume_spike")
        if long_crowding_score > 0.75:
            risk_flags.append("long_crowding")
        if short_crowding_score > 0.75:
            risk_flags.append("short_crowding")
        if liquidation_risk_proxy > 0.8:
            risk_flags.append("high_liquidation_risk")
        if breakout_distance_pct is not None and breakout_distance_pct < 0.8:
            risk_flags.append("near_breakout_boundary")
        if (screening.get("data_quality_score") or 0) < 0.8:
            risk_flags.append("imperfect_data_quality")
        return list(dict.fromkeys(risk_flags))

    async def _fetch_binance_klines(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        interval: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        try:
            payload = await self._fetch_json(
                session,
                f"{BINANCE_FAPI_URL}/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit},
            )
        except Exception:
            return []
        candles = []
        for row in payload or []:
            if not isinstance(row, list) or len(row) < 11:
                continue
            candles.append(
                {
                    "open_time": row[0],
                    "open": safe_float(row[1]),
                    "high": safe_float(row[2]),
                    "low": safe_float(row[3]),
                    "close": safe_float(row[4]),
                    "volume": safe_float(row[5]),
                    "close_time": row[6],
                    "quote_volume": safe_float(row[7]),
                    "trade_count": safe_float(row[8]),
                    "taker_buy_base_volume": safe_float(row[9]),
                    "taker_buy_quote_volume": safe_float(row[10]),
                }
            )
        return candles

    async def _fetch_funding_rate_history(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        try:
            payload = await self._fetch_json(
                session,
                f"{BINANCE_FAPI_URL}/fapi/v1/fundingRate",
                {"symbol": symbol, "limit": limit},
            )
        except Exception:
            return []
        items = []
        for row in payload or []:
            funding_rate = safe_float(row.get("fundingRate"))
            funding_time = safe_float(row.get("fundingTime"))
            if funding_rate is None or funding_time is None:
                continue
            items.append(
                {
                    "funding_time": funding_time,
                    "funding_rate": funding_rate,
                    "funding_rate_pct": funding_rate * 100,
                    "mark_price": safe_float(row.get("markPrice")),
                }
            )
        return items

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
