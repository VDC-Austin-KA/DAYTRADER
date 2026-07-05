"""SQLAlchemy ORM models for the paper-trading platform."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, default="default")
    cash: Mapped[float] = mapped_column(Float, default=100000.0)
    starting_cash: Mapped[float] = mapped_column(Float, default=100000.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    positions: Mapped[list["Position"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    trades: Mapped[list["Trade"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class Position(Base):
    """An open options position (paper)."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"))

    symbol: Mapped[str] = mapped_column(String(20), index=True)
    contract_symbol: Mapped[str] = mapped_column(String(40), index=True)
    option_type: Mapped[str] = mapped_column(String(4))  # call / put
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD

    quantity: Mapped[int] = mapped_column(Integer)  # number of contracts
    entry_price: Mapped[float] = mapped_column(Float)  # per share premium
    entry_underlying: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open/closed
    note: Mapped[str] = mapped_column(Text, default="")

    portfolio: Mapped[Portfolio] = relationship(back_populates="positions")

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity * 100

    @property
    def market_value(self) -> float:
        return self.current_price * self.quantity * 100

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis


class Trade(Base):
    """A historical fill (open or close)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"))

    symbol: Mapped[str] = mapped_column(String(20), index=True)
    contract_symbol: Mapped[str] = mapped_column(String(40))
    side: Mapped[str] = mapped_column(String(8))  # buy / sell
    option_type: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    portfolio: Mapped[Portfolio] = relationship(back_populates="trades")


class Signal(Base):
    """A model-generated trade idea, cached for the dashboard."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("symbol", name="uq_signal_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # bullish/bearish/neutral
    probability: Mapped[float] = mapped_column(Float)  # model confidence 0-1
    underlying_price: Mapped[float] = mapped_column(Float, default=0.0)

    option_type: Mapped[str] = mapped_column(String(4), default="")
    contract_symbol: Mapped[str] = mapped_column(String(40), default="")
    strike: Mapped[float] = mapped_column(Float, default=0.0)
    expiry: Mapped[str] = mapped_column(String(10), default="")
    dte: Mapped[int] = mapped_column(Integer, default=0)
    option_price: Mapped[float] = mapped_column(Float, default=0.0)
    breakeven: Mapped[float] = mapped_column(Float, default=0.0)

    rationale: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PredictionTrade(Base):
    """A BTC hourly prediction-market contract trade placed by the bot."""

    __tablename__ = "prediction_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(60), index=True)
    series: Mapped[str] = mapped_column(String(20), default="")
    side: Mapped[str] = mapped_column(String(3))  # yes / no
    strike: Mapped[float] = mapped_column(Float, default=0.0)
    close_time: Mapped[datetime] = mapped_column(DateTime, index=True)

    probability: Mapped[float] = mapped_column(Float)  # model P(side wins)
    entry_price: Mapped[float] = mapped_column(Float)  # dollars per contract
    quantity: Mapped[int] = mapped_column(Integer)
    stake: Mapped[float] = mapped_column(Float, default=0.0)

    mode: Mapped[str] = mapped_column(String(6), default="paper")  # paper / live
    order_id: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open/settled/error
    result: Mapped[str] = mapped_column(String(6), default="")  # win / loss / void
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    settlement_price: Mapped[float] = mapped_column(Float, default=0.0)  # BTC index

    rationale: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PredictionBotState(Base):
    """Singleton row with the bot's operator-facing state."""

    __tablename__ = "prediction_bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paused: Mapped[int] = mapped_column(Integer, default=0)  # 0/1
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ModelMeta(Base):
    """Tracks the latest trained model's metrics per symbol."""

    __tablename__ = "model_meta"
    __table_args__ = (UniqueConstraint("symbol", name="uq_model_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    roc_auc: Mapped[float] = mapped_column(Float, default=0.0)
    n_samples: Mapped[int] = mapped_column(Integer, default=0)
    trained_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    features: Mapped[str] = mapped_column(Text, default="")
