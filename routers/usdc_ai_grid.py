from fastapi import APIRouter, HTTPException

from models.usdc_ai_grid import DeployGridPlanRequest, DeployGridPlanResponse, GridPlanValidationResponse, ValidateGridPlanRequest
from services.usdc_ai_grid_plan_service import USDCAIGridPlanService


router = APIRouter(tags=["USDC AI Grid"], prefix="/usdc-ai-grid")


def get_service() -> USDCAIGridPlanService:
    return USDCAIGridPlanService()


@router.post("/plan/validate", response_model=GridPlanValidationResponse)
async def validate_plan(request: ValidateGridPlanRequest):
    return get_service().validate_plan(request.plan, request.candidate)


@router.post("/plan/preview")
async def preview_plan(request: ValidateGridPlanRequest):
    return get_service().preview_plan(request.plan, request.candidate)


@router.post("/plan/deploy", response_model=DeployGridPlanResponse)
async def deploy_plan(request: DeployGridPlanRequest):
    service = get_service()
    validation = service.validate_plan(request.plan, request.candidate)
    if not validation.valid:
        return DeployGridPlanResponse(dry_run=request.dry_run, validation=validation)
    if request.dry_run:
        return DeployGridPlanResponse(
            dry_run=True,
            validation=validation,
            executor_config=validation.executor_config,
        )
    raise HTTPException(status_code=501, detail="Live deploy must be wired to ExecutorService after dry-run validation.")


@router.post("/executors/review")
async def review_executors():
    return {"status": "not_implemented", "detail": "Use Condor local review routines until audit metrics are finalized."}

