from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class USDCUniverseRequest(BaseModel):
    connector_name: str = "binance_perpetual"
    quote_asset: str = "USDC"
    max_pairs: int = Field(default=32, ge=1, le=200)
    min_24h_quote_volume: float = Field(default=0, ge=0)


class USDCUniverseResponse(BaseModel):
    connector_name: str
    quote_asset: str
    trading_pairs: List[str]
    markets: List[Dict[str, Any]]


class USDCMarketSnapshotRequest(BaseModel):
    connector_name: str = "binance_perpetual"
    trading_pairs: List[str]
    interval: str = "1h"
    max_records: int = Field(default=72, ge=10, le=1000)
    order_book_depth: int = Field(default=100, ge=1, le=1000)


class USDCPerpPressureRequest(BaseModel):
    connector_name: str = "binance_perpetual"
    trading_pairs: List[str]


class USDCCandidatesRequest(USDCMarketSnapshotRequest):
    min_score: int = Field(default=60, ge=0, le=100)


class PerpPressure(BaseModel):
    trading_pair: str
    symbol: str
    funding_rate: Optional[float] = None
    funding_rate_pct: Optional[float] = None
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    basis_pct: Optional[float] = None
    open_interest: Optional[float] = None
    errors: List[str] = Field(default_factory=list)


class MarketSnapshotItem(BaseModel):
    trading_pair: str
    price: Optional[float] = None
    candles: List[Dict[str, Any]] = Field(default_factory=list)
    order_book: Dict[str, Any] = Field(default_factory=dict)
    funding_info: Dict[str, Any] = Field(default_factory=dict)
    trading_rule: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)


class CandidateFeature(BaseModel):
    trading_pair: str
    score: int
    tradable: bool
    market_regime: str
    price: Optional[float] = None
    spread_bps: Optional[float] = None
    order_book_depth_20bps_quote: float = 0
    order_book_depth_50bps_quote: float = 0
    funding_rate_pct: Optional[float] = None
    basis_pct: Optional[float] = None
    open_interest: Optional[float] = None
    reason_codes: List[str] = Field(default_factory=list)
    market_alerts: List[str] = Field(default_factory=list)

