import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Currency, OutboxEvent, Payment, PaymentStatus


async def insert_payment(
    session: AsyncSession,
    *,
    idempotency_key: str,
    amount: Decimal,
    currency: Currency,
    description: str | None,
    metadata: dict[str, Any],
    webhook_url: str,
) -> Payment | None:
    return await session.scalar(
        insert(Payment)
        .values(
            idempotency_key=idempotency_key,
            amount=amount,
            currency=currency,
            description=description,
            metadata_=metadata,
            webhook_url=webhook_url,
        )
        .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
        .returning(Payment)
    )


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    return await session.get(Payment, payment_id)


async def get_payment_by_key(session: AsyncSession, idempotency_key: str) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))


async def set_final_status(
    session: AsyncSession, payment_id: uuid.UUID, status: PaymentStatus
) -> Payment:
    updated: Payment | None = await session.scalar(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status == PaymentStatus.PENDING)
        .values(status=status, processed_at=func.now())
        .returning(Payment)
    )
    if updated is None:
        return await session.get_one(Payment, payment_id, populate_existing=True)
    return updated


def add_outbox_event(session: AsyncSession, event_type: str, payload: dict[str, Any]) -> None:
    session.add(OutboxEvent(event_type=event_type, payload=payload))


async def lock_unpublished_events(session: AsyncSession, limit: int) -> Sequence[OutboxEvent]:
    result = await session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return result.all()


def mark_published(event: OutboxEvent) -> None:
    event.published_at = func.now()
