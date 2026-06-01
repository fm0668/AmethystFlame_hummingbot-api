from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("hummingbot")

from services.usdc_perp_market_service import USDCPerpMarketService


def _build_market(errors=None):
    candles = [{"close": 100 + (i % 3)} for i in range(24)]
    return {
        "trading_pair": "BTC-USDC",
        "price": 100.5,
        "candles": candles,
        "order_book": {
            "bids": [[100.0, 200.0], [99.9, 100.0]],
            "asks": [[101.0, 150.0], [101.1, 120.0]],
        },
        "trading_rule": {"min_notional_size": 5.0},
        "errors": errors or [],
    }


def _build_kline_series(length, start_price=100.0, price_step=0.2, quote_volume=100000.0):
    candles = []
    for index in range(length):
        open_price = start_price + price_step * index
        close_price = open_price + (0.3 if index % 2 == 0 else -0.1)
        high_price = max(open_price, close_price) * 1.002
        low_price = min(open_price, close_price) * 0.998
        candles.append(
            {
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "quote_volume": quote_volume + index * 1000,
                "trade_count": 200 + index,
            }
        )
    return candles


def _build_pressure(open_interest=20000.0, errors=None):
    history = []
    for index in range(48):
        history.append(
            {
                "timestamp": index,
                "open_interest": open_interest - 500 + index * 10,
                "open_interest_value": open_interest * 100,
            }
        )
    return {
        "funding_rate_pct": 0.01,
        "basis_pct": 0.01,
        "open_interest": open_interest,
        "open_interest_change_pct_1h": 2.5,
        "open_interest_history": history,
        "errors": errors or [],
    }


def test_compute_feature_supports_array_order_books():
    service = USDCPerpMarketService(MagicMock())

    feature = service._compute_feature(
        _build_market(errors=["candles: warmup timeout"]),
        {
            "funding_rate_pct": 0.01,
            "basis_pct": 0.01,
            "open_interest": 12345.0,
            "open_interest_change_pct_5m": 1.2,
            "open_interest_history": [{"timestamp": 1, "open_interest": 12000.0}],
            "errors": ["funding_info: stale cache"],
        },
    )

    assert feature["price"] == 100.5
    assert feature["spread_bps"] == pytest.approx((101.0 - 100.0) / 100.5 * 10000)
    assert feature["order_book_depth_20bps_quote"] > 0
    assert feature["order_book_depth_50bps_quote"] > 0
    assert feature["trading_rule"]["min_notional_size"] == 5.0
    assert feature["open_interest_change_pct_5m"] == 1.2
    assert feature["snapshot_errors"] == ["candles: warmup timeout", "funding_info: stale cache"]


@pytest.mark.asyncio
async def test_get_candidates_marks_snapshot_errors_non_tradable():
    service = USDCPerpMarketService(MagicMock())
    service.get_snapshot = AsyncMock(
        return_value={
            "markets": {
                "BTC-USDC": _build_market(errors=["order_book: empty bids or asks"]),
            }
        }
    )
    service.get_perp_pressure = AsyncMock(
        return_value={
            "markets": {
                "BTC-USDC": {
                    "funding_rate_pct": 0.0,
                    "basis_pct": 0.0,
                    "open_interest": 20000.0,
                    "errors": [],
                }
            }
        }
    )

    result = await service.get_candidates(
        connector_name="binance_perpetual",
        trading_pairs=["BTC-USDC"],
        min_score=0,
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["tradable"] is False
    assert result["candidates"][0]["snapshot_errors"] == ["order_book: empty bids or asks"]


def test_build_screening_candidate_respects_min_score():
    service = USDCPerpMarketService(MagicMock())

    screening = service._build_screening_candidate(
        universe_market={"trading_pair": "BTC-USDC", "quote_volume_24h": 1200000.0, "last_price": 100.5},
        candidate={
            "trading_pair": "BTC-USDC",
            "score": 70,
            "market_regime": "range",
            "price": 100.5,
            "spread_bps": 2.0,
            "order_book_depth_20bps_quote": 80000.0,
            "order_book_depth_50bps_quote": 120000.0,
            "funding_rate_pct": 0.01,
            "basis_pct": 0.01,
            "open_interest_change_pct_1h": 2.0,
            "reason_codes": ["spread_ok"],
            "market_alerts": [],
            "snapshot_errors": [],
            "trading_rule": {"min_notional_size": 5.0},
        },
        market=_build_market(),
        pressure=_build_pressure(),
        candles_15m=_build_kline_series(96),
        candles_1h=_build_kline_series(72, price_step=0.5, quote_volume=150000.0),
        min_score=80,
    )

    assert screening["tradable"] is False
    assert "low_candidate_score" in screening["exclusion_reasons"]


@pytest.mark.asyncio
async def test_get_decision_candidates_returns_structured_ai_inputs():
    service = USDCPerpMarketService(MagicMock())
    service.get_universe = AsyncMock(
        return_value={
            "trading_pairs": ["BTC-USDC", "DOGE-USDC"],
            "markets": [
                {"trading_pair": "BTC-USDC", "quote_volume_24h": 2500000.0, "last_price": 100.5},
                {"trading_pair": "DOGE-USDC", "quote_volume_24h": 150000.0, "last_price": 1.0},
            ],
        }
    )
    service.get_snapshot = AsyncMock(
        return_value={
            "markets": {
                "BTC-USDC": _build_market(),
                "DOGE-USDC": {
                    **_build_market(errors=["order_book: empty bids or asks"]),
                    "trading_pair": "DOGE-USDC",
                    "price": 1.0,
                    "order_book": {
                        "bids": [[1.0, 100.0]],
                        "asks": [[1.02, 50.0]],
                    },
                },
            }
        }
    )
    service.get_perp_pressure = AsyncMock(
        return_value={
            "markets": {
                "BTC-USDC": _build_pressure(),
                "DOGE-USDC": _build_pressure(open_interest=5000.0, errors=["funding_info: stale cache"]),
            }
        }
    )
    service._fetch_kline_batch = AsyncMock(
        side_effect=[
            {
                "BTC-USDC": _build_kline_series(96),
                "DOGE-USDC": _build_kline_series(96, start_price=1.0, price_step=0.01, quote_volume=5000.0),
            },
            {
                "BTC-USDC": _build_kline_series(72, price_step=0.5, quote_volume=150000.0),
                "DOGE-USDC": _build_kline_series(72, start_price=1.0, price_step=0.02, quote_volume=8000.0),
            },
            {
                "BTC-USDC": _build_kline_series(96, price_step=0.05, quote_volume=90000.0),
            },
        ]
    )
    service._fetch_funding_rate_batch = AsyncMock(
        return_value={
            "BTC-USDC": [
                {"funding_rate_pct": 0.008 + index * 0.0002, "funding_time": index}
                for index in range(12)
            ]
        }
    )

    result = await service.get_decision_candidates(
        connector_name="binance_perpetual",
        top_n=1,
        min_score=60,
    )

    assert result["universe"]["selected_count"] == 1
    assert result["watch_pool"] == ["BTC-USDC"]
    assert result["excluded_pairs"][0]["symbol"] == "DOGE-USDC"

    candidate = result["decision_candidates"][0]
    assert candidate["symbol"] == "BTC-USDC"
    assert candidate["recommended_grid_modes"]
    assert candidate["liquidity"]["depth_50bps_quote"] > 0
    assert candidate["volatility"]["atr_15m_pct"] is not None
    assert candidate["volume"]["volume_1h_zscore"] is not None
    assert candidate["perp_pressure"]["funding_rate_zscore"] is not None
    assert candidate["execution"]["max_grid_levels"] >= 3
