import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from investment_town.schemas.control import ControlEvent

OrderSide = Literal["buy", "sell"]
INITIAL_CASH_CENTS = 10_000_000


class PaperOrderRequest(BaseModel):
    project_id: str = "investment-town"
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.-]+$")
    side: OrderSide
    quantity: int = Field(gt=0, le=1_000_000)
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()


class PaperPosition(BaseModel):
    ticker: str
    quantity: int
    average_cost: Decimal
    cost_basis: Decimal


class PaperTrade(BaseModel):
    trade_id: UUID
    project_id: str
    ticker: str
    side: OrderSide
    quantity: int
    price: Decimal
    total: Decimal
    realized_pnl: Decimal
    reason: str | None
    created_at: datetime


class PaperPortfolio(BaseModel):
    project_id: str
    currency: str
    initial_cash: Decimal
    cash: Decimal
    positions_cost_basis: Decimal
    book_value: Decimal
    realized_pnl: Decimal
    positions: list[PaperPosition]
    updated_at: datetime


class PaperOrderResult(BaseModel):
    portfolio: PaperPortfolio
    trade: PaperTrade
    event: ControlEvent


class InvalidPaperOrder(ValueError):
    pass


def _money(cents: int) -> Decimal:
    return Decimal(cents) / 100


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PaperStore:
    """Single-account, whole-share paper trading for the SQLite MVP."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    project_id TEXT PRIMARY KEY,
                    currency TEXT NOT NULL,
                    initial_cash_cents INTEGER NOT NULL,
                    cash_cents INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    project_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    average_cost_cents INTEGER NOT NULL,
                    PRIMARY KEY(project_id, ticker)
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price_cents INTEGER NOT NULL,
                    total_cents INTEGER NOT NULL,
                    realized_pnl_cents INTEGER NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        self._connection.close()

    def _ensure_account(self, project_id: str) -> sqlite3.Row:
        project = self._connection.execute(
            "SELECT state FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        now = _now()
        self._connection.execute(
            """
            INSERT OR IGNORE INTO paper_accounts(
                project_id, currency, initial_cash_cents, cash_cents, updated_at
            ) VALUES (?, 'USD', ?, ?, ?)
            """,
            (project_id, INITIAL_CASH_CENTS, INITIAL_CASH_CENTS, now),
        )
        return project

    def get_portfolio(self, project_id: str) -> PaperPortfolio:
        with self._lock, self._connection:
            self._ensure_account(project_id)
            account = self._connection.execute(
                "SELECT * FROM paper_accounts WHERE project_id = ?", (project_id,)
            ).fetchone()
            rows = self._connection.execute(
                "SELECT * FROM paper_positions WHERE project_id = ? ORDER BY ticker",
                (project_id,),
            ).fetchall()
            realized = self._connection.execute(
                """
                SELECT COALESCE(SUM(realized_pnl_cents), 0)
                FROM paper_trades WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()[0]

        positions = [
            PaperPosition(
                ticker=row["ticker"],
                quantity=row["quantity"],
                average_cost=_money(row["average_cost_cents"]),
                cost_basis=_money(row["quantity"] * row["average_cost_cents"]),
            )
            for row in rows
        ]
        cost_basis_cents = sum(row["quantity"] * row["average_cost_cents"] for row in rows)
        return PaperPortfolio(
            project_id=project_id,
            currency=account["currency"],
            initial_cash=_money(account["initial_cash_cents"]),
            cash=_money(account["cash_cents"]),
            positions_cost_basis=_money(cost_basis_cents),
            book_value=_money(account["cash_cents"] + cost_basis_cents),
            realized_pnl=_money(realized),
            positions=positions,
            updated_at=account["updated_at"],
        )

    def list_trades(self, project_id: str, limit: int = 100) -> list[PaperTrade]:
        with self._lock, self._connection:
            self._ensure_account(project_id)
            rows = self._connection.execute(
                """
                SELECT * FROM paper_trades
                WHERE project_id = ? ORDER BY sequence DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [self._trade(row) for row in rows]

    def submit(self, order: PaperOrderRequest) -> PaperOrderResult:
        price_cents = int(order.price * 100)
        total_cents = price_cents * order.quantity
        trade_id = uuid4()
        event_id = uuid4()
        created_at = _now()

        with self._lock, self._connection:
            project = self._ensure_account(order.project_id)
            if project["state"] != "running":
                raise InvalidPaperOrder("project must be running to submit a paper order")

            account = self._connection.execute(
                "SELECT cash_cents FROM paper_accounts WHERE project_id = ?",
                (order.project_id,),
            ).fetchone()
            position = self._connection.execute(
                """
                SELECT quantity, average_cost_cents FROM paper_positions
                WHERE project_id = ? AND ticker = ?
                """,
                (order.project_id, order.ticker),
            ).fetchone()
            old_quantity = position["quantity"] if position else 0
            old_average = position["average_cost_cents"] if position else 0
            realized_pnl_cents = 0

            if order.side == "buy":
                if account["cash_cents"] < total_cents:
                    raise InvalidPaperOrder("insufficient paper cash")
                new_quantity = old_quantity + order.quantity
                new_average = (
                    old_quantity * old_average + order.quantity * price_cents + new_quantity // 2
                ) // new_quantity
                new_cash = account["cash_cents"] - total_cents
                self._connection.execute(
                    """
                    INSERT INTO paper_positions(project_id, ticker, quantity, average_cost_cents)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_id, ticker) DO UPDATE SET
                        quantity = excluded.quantity,
                        average_cost_cents = excluded.average_cost_cents
                    """,
                    (order.project_id, order.ticker, new_quantity, new_average),
                )
            else:
                if old_quantity < order.quantity:
                    raise InvalidPaperOrder("insufficient paper position")
                new_quantity = old_quantity - order.quantity
                new_cash = account["cash_cents"] + total_cents
                realized_pnl_cents = (price_cents - old_average) * order.quantity
                if new_quantity:
                    self._connection.execute(
                        """
                        UPDATE paper_positions SET quantity = ?
                        WHERE project_id = ? AND ticker = ?
                        """,
                        (new_quantity, order.project_id, order.ticker),
                    )
                else:
                    self._connection.execute(
                        "DELETE FROM paper_positions WHERE project_id = ? AND ticker = ?",
                        (order.project_id, order.ticker),
                    )

            self._connection.execute(
                "UPDATE paper_accounts SET cash_cents = ?, updated_at = ? WHERE project_id = ?",
                (new_cash, created_at, order.project_id),
            )
            trade_cursor = self._connection.execute(
                """
                INSERT INTO paper_trades(
                    trade_id, project_id, ticker, side, quantity, price_cents,
                    total_cents, realized_pnl_cents, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(trade_id), order.project_id, order.ticker, order.side, order.quantity,
                    price_cents, total_cents, realized_pnl_cents, order.reason, created_at,
                ),
            )
            payload = {
                "trade_id": str(trade_id),
                "ticker": order.ticker,
                "side": order.side,
                "quantity": order.quantity,
                "price": str(order.price),
                "total": str(_money(total_cents)),
                "realized_pnl": str(_money(realized_pnl_cents)),
            }
            event_cursor = self._connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_id, payload, created_at)
                VALUES (?, 'paper.order.filled', ?, ?, ?)
                """,
                (str(event_id), order.project_id, json.dumps(payload), created_at),
            )
            trade_row = self._connection.execute(
                "SELECT * FROM paper_trades WHERE sequence = ?", (trade_cursor.lastrowid,)
            ).fetchone()

        event = ControlEvent(
            sequence=event_cursor.lastrowid,
            event_id=event_id,
            event_type="paper.order.filled",
            project_id=order.project_id,
            payload=payload,
            created_at=created_at,
        )
        return PaperOrderResult(
            portfolio=self.get_portfolio(order.project_id),
            trade=self._trade(trade_row),
            event=event,
        )

    @staticmethod
    def _trade(row: sqlite3.Row) -> PaperTrade:
        return PaperTrade(
            trade_id=row["trade_id"],
            project_id=row["project_id"],
            ticker=row["ticker"],
            side=row["side"],
            quantity=row["quantity"],
            price=_money(row["price_cents"]),
            total=_money(row["total_cents"]),
            realized_pnl=_money(row["realized_pnl_cents"]),
            reason=row["reason"],
            created_at=row["created_at"],
        )
