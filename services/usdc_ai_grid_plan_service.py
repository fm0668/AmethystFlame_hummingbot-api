from typing import Any, Dict, Optional

from models.usdc_ai_grid import GridPlanValidationResponse, USDCGridPlan
from services.grid_audit_service import GridAuditService


class USDCAIGridPlanService:
    def __init__(self, audit_service: Optional[GridAuditService] = None):
        self.audit_service = audit_service or GridAuditService()

    def validate_plan(self, plan: USDCGridPlan, candidate: Optional[Dict[str, Any]] = None) -> GridPlanValidationResponse:
        errors = []
        warnings = []

        if plan.grid_mode not in {"long_grid", "short_grid", "neutral_grid"}:
            errors.append("grid_mode must be long_grid, short_grid, or neutral_grid")
        if plan.start_price == plan.end_price:
            errors.append("start_price and end_price must differ")
        if plan.start_price > plan.end_price:
            warnings.append("start_price is above end_price; bounds will be normalized by the execution layer")
        if plan.min_spread_between_orders < 0.004:
            errors.append("min_spread_between_orders must be >= 0.004")
        if not plan.decision_reason.strip():
            errors.append("decision_reason is required")
        if not plan.invalidation_conditions:
            errors.append("at least one invalidation condition is required")
        if plan.grid_mode == "neutral_grid":
            warnings.append(
                "neutral_grid is preview-compatible only; live execution still needs controller-level orchestration"
            )

        candidate_min_order_quote = self._candidate_min_order_amount_quote(candidate)
        if candidate:
            if candidate.get("tradable") is False:
                warnings.append("candidate is not marked tradable")
            per_order = plan.total_amount_quote / plan.max_open_orders
            if candidate_min_order_quote and per_order < candidate_min_order_quote:
                errors.append(
                    "per-order quote amount "
                    f"{per_order:.4f} is below min-notional buffer {candidate_min_order_quote:.4f}"
                )
            alerts = candidate.get("market_alerts") or []
            if alerts:
                warnings.append(f"candidate alerts: {', '.join(alerts)}")
            snapshot_errors = candidate.get("snapshot_errors") or []
            if snapshot_errors:
                warnings.append(f"candidate snapshot errors: {', '.join(snapshot_errors)}")

        executor_config = None if errors else self.to_executor_config(plan, candidate)
        response = GridPlanValidationResponse(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            executor_config=executor_config,
        )
        self.audit_service.write_record("validate_plan", {"plan": plan.model_dump(), "response": response.model_dump()})
        return response

    def preview_plan(self, plan: USDCGridPlan, candidate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        validation = self.validate_plan(plan, candidate)
        return {"validation": validation.model_dump(), "executor_config": validation.executor_config}

    def to_executor_config(self, plan: USDCGridPlan, candidate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        side = self._executor_side(plan.grid_mode)
        min_order_amount_quote = self._candidate_min_order_amount_quote(candidate) or 5.0
        return {
            "type": "grid_executor",
            "connector_name": plan.connector_name,
            "trading_pair": plan.trading_pair,
            "grid_mode": plan.grid_mode,
            "start_price": plan.start_price,
            "end_price": plan.end_price,
            "limit_price": plan.limit_price or self._default_limit_price(plan),
            "side": side["value"],
            "side_name": side["name"],
            "total_amount_quote": plan.total_amount_quote,
            "min_spread_between_orders": plan.min_spread_between_orders,
            "min_order_amount_quote": min_order_amount_quote,
            "max_open_orders": plan.max_open_orders,
            "leverage": plan.leverage,
            "triple_barrier_config": plan.triple_barrier_config.model_dump(),
            "execution_supported": plan.grid_mode != "neutral_grid",
        }

    @staticmethod
    def _default_limit_price(plan: USDCGridPlan) -> float:
        if plan.grid_mode == "short_grid":
            return max(plan.start_price, plan.end_price)
        return min(plan.start_price, plan.end_price)

    @staticmethod
    def _executor_side(grid_mode: str) -> Dict[str, Any]:
        if grid_mode == "short_grid":
            return {"name": "SELL", "value": 2}
        return {"name": "BUY", "value": 1}

    @staticmethod
    def _candidate_min_order_amount_quote(candidate: Optional[Dict[str, Any]]) -> float:
        if not candidate:
            return 0.0
        trading_rule = candidate.get("trading_rule") or {}
        min_notional = trading_rule.get("min_notional_size") or trading_rule.get("min_order_value") or 0
        try:
            return float(min_notional) * 1.2
        except (TypeError, ValueError):
            return 0.0
