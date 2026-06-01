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
