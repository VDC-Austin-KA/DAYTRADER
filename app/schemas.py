"""Pydantic request/response schemas for the JSON API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TradeRequest(BaseModel):
    symbol: str
    option_type: str = Field(pattern="^(call|put)$")
    contract_symbol: str
    strike: float
    expiry: str
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    note: str = ""


class CloseRequest(BaseModel):
    position_id: int
    price: float | None = None


class TrainRequest(BaseModel):
    symbols: list[str] | None = None


class SignalOut(BaseModel):
    symbol: str
    direction: str
    probability: float
    underlying_price: float
    option_type: str
    contract_symbol: str
    strike: float
    expiry: str
    dte: int
    option_price: float
    breakeven: float
    rationale: str

    class Config:
        from_attributes = True
