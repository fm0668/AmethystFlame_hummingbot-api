from unittest.mock import MagicMock

from models.usdc_ai_grid import TripleBarrierConfig, USDCGridPlan
from services.usdc_ai_grid_plan_service import USDCAIGridPlanService


def _build_plan(grid_mode="long_grid", total_amount_quote=120.0, max_open_orders=6, limit_price=None):
    return USDCGridPlan(
        connector_name="binance_perpetual",
        trading_pair="BTC-USDC",
        grid_mode=grid_mode,
        start_price=100.0,
        end_price=110.0,
        limit_price=limit_price,
        total_amount_quote=total_amount_quote,
        min_spread_between_orders=0.004,
        max_open_orders=max_open_orders,
        leverage=2,
        triple_barrier_config=TripleBarrierConfig(stop_loss=0.03, take_profit=0.02, time_limit=3600),
        decision_reason="range setup",
        invalidation_conditions=["breakout"],
    )


def test_validate_plan_blocks_too_small_per_order_quote():
    service = USDCAIGridPlanService(audit_service=MagicMock())
    plan = _build_plan(total_amount_quote=20.0, max_open_orders=4)

    response = service.validate_plan(
        plan,
        candidate={
            "trading_rule": {"min_notional_size": 10.0},
            "market_alerts": ["thin_depth_50bps"],
            "snapshot_errors": ["candles: warmup timeout"],
        },
    )

    assert response.valid is False
    assert "below min-notional buffer" in response.errors[0]
    assert "candidate alerts: thin_depth_50bps" in response.warnings
    assert "candidate snapshot errors: candles: warmup timeout" in response.warnings


def test_to_executor_config_maps_short_grid_and_defaults_limit_price():
    service = USDCAIGridPlanService(audit_service=MagicMock())
    plan = _build_plan(grid_mode="short_grid", limit_price=None)

    executor_config = service.to_executor_config(
        plan,
        candidate={"trading_rule": {"min_notional_size": 7.5}},
    )

    assert executor_config["grid_mode"] == "short_grid"
    assert executor_config["side"] == 2
    assert executor_config["side_name"] == "SELL"
    assert executor_config["limit_price"] == 110.0
    assert executor_config["min_order_amount_quote"] == 9.0
    assert executor_config["execution_supported"] is True


def test_neutral_grid_preview_is_marked_preview_only():
    service = USDCAIGridPlanService(audit_service=MagicMock())
    plan = _build_plan(grid_mode="neutral_grid")

    response = service.validate_plan(plan)

    assert response.valid is True
    assert any("preview-compatible only" in warning for warning in response.warnings)
    assert response.executor_config["execution_supported"] is False
    assert response.executor_config["side"] == 1
