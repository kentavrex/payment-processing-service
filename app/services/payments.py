import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import repository
from app.database.models import Payment
from app.schemas import PaymentCreate


class IdempotencyKeyReused(Exception):
    pass


def _same_request(payment: Payment, data: PaymentCreate) -> bool:
    return (
        payment.amount == data.amount
        and payment.currency == data.currency
        and payment.description == data.description
        and payment.metadata_ == data.metadata
        and payment.webhook_url == str(data.webhook_url)
    )


async def create_payment(session: AsyncSession, key: str, data: PaymentCreate) -> Payment:
    payment = await repository.insert_payment(
        session,
        idempotency_key=key,
        amount=data.amount,
        currency=data.currency,
        description=data.description,
        metadata=data.metadata,
        webhook_url=str(data.webhook_url),
    )

    if payment is None:
        existing = await repository.get_payment_by_key(session, key)
        if existing is None or not _same_request(existing, data):
            raise IdempotencyKeyReused
        return existing

    repository.add_outbox_event(session, "payment.created", {"payment_id": str(payment.id)})
    await session.commit()
    return payment


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    return await repository.get_payment(session, payment_id)
