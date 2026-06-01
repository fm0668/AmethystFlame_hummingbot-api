from fastapi import APIRouter, Depends

from deps import get_market_data_service
from models.usdc_perp_market import (
    USDCCandidatesRequest,
    USDCMarketSnapshotRequest,
    USDCPerpPressureRequest,
    USDCUniverseRequest,
)
from services.market_data_service import MarketDataService
from services.usdc_perp_market_service import USDCPerpMarketService


router = APIRouter(tags=["USDC Perp Market"], prefix="/usdc-perp-market")


def get_service(market_data_service: MarketDataService = Depends(get_market_data_service)) -> USDCPerpMarketService:
    return USDCPerpMarketService(market_data_service)


@router.get("/universe")
async def get_universe(
    connector_name: str = "binance_perpetual",
    quote_asset: str = "USDC",
    max_pairs: int = 32,
    min_24h_quote_volume: float = 0,
    service: USDCPerpMarketService = Depends(get_service),
):
    request = USDCUniverseRequest(
        connector_name=connector_name,
        quote_asset=quote_asset,
        max_pairs=max_pairs,
        min_24h_quote_volume=min_24h_quote_volume,
    )
    return await service.get_universe(**request.model_dump())


@router.post("/snapshot")
async def get_snapshot(request: USDCMarketSnapshotRequest, service: USDCPerpMarketService = Depends(get_service)):
    return await service.get_snapshot(**request.model_dump())


@router.post("/perp-pressure")
async def get_perp_pressure(request: USDCPerpPressureRequest, service: USDCPerpMarketService = Depends(get_service)):
    return await service.get_perp_pressure(**request.model_dump())


@router.post("/candidates")
async def get_candidates(request: USDCCandidatesRequest, service: USDCPerpMarketService = Depends(get_service)):
    return await service.get_candidates(**request.model_dump())

