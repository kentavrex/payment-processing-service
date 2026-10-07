import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Enum, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Currency(enum.StrEnum):
    RUB = "RUB"
    USD = "USD"
    EUR = "EUR"


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _string_enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


def _one_of(column: str, cls: type[enum.StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in cls)
    return CheckConstraint(f"{column} IN ({values})", name=column)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        _one_of("currency", Currency),
        _one_of("status", PaymentStatus),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[Currency] = mapped_column(_string_enum(Currency, "currency"))
    description: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    status: Mapped[PaymentStatus] = mapped_column(
        _string_enum(PaymentStatus, "status"), server_default=PaymentStatus.PENDING.value
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    webhook_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    processed_at: Mapped[datetime | None]


class OutboxEvent(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_unpublished", "created_at", postgresql_where=text("published_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    published_at: Mapped[datetime | None]
