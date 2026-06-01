from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TripleBarrierConfig(BaseModel):
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    time_limit: int = Field(ge=60)


class USDCGridPlan(BaseModel):
    connector_name: str = "binance_perpetual"
    trading_pair: str
    grid_mode: str
    start_price: float = Field(gt=0)
    end_price: float = Field(gt=0)
    limit_price: Optional[float] = None
    total_amount_quote: float = Field(gt=0)
    min_spread_between_orders: float = Field(gt=0)
    max_open_orders: int = Field(ge=2, le=50)
    leverage: int = Field(ge=1, le=20)
    triple_barrier_config: TripleBarrierConfig
    decision_reason: str
    invalidation_conditions: List[str] = Field(default_factory=list)
    source_candidate_file: Optional[str] = None
    created_at: Optional[str] = None


class ValidateGridPlanRequest(BaseModel):
    plan: USDCGridPlan
    candidate: Optional[Dict[str, Any]] = None


class GridPlanValidationResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    executor_config: Optional[Dict[str, Any]] = None


class DeployGridPlanRequest(ValidateGridPlanRequest):
    account_name: str = "master_account"
    controller_id: str = "usdc_ai_grid"
    dry_run: bool = True


class DeployGridPlanResponse(BaseModel):
    dry_run: bool
    validation: GridPlanValidationResponse
    executor_config: Optional[Dict[str, Any]] = None
    executor_result: Optional[Dict[str, Any]] = None

