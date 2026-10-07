import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import status
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from app.database.models import Currency, OutboxEvent, Payment, PaymentStatus
from app.database.session import SessionFactory
from app.schemas import PaymentCreate

URL = "/api/v1/payments"


def body(**overrides: Any) -> dict[str, Any]:
    return {
        "amount": "100.00",
        "currency": "RUB",
        "description": "Order 42",
        "metadata": {"order_id": 42},
        "webhook_url": "https://example.com/hook",
    } | overrides


async def create(client: AsyncClient, key: str = "key-1", **overrides: Any) -> Response:
    return await client.post(URL, json=body(**overrides), headers={"Idempotency-Key": key})


async def count(model: type[Payment] | type[OutboxEvent]) -> int:
    async with SessionFactory() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_create_payment_stores_payment_and_outbox_event(client: AsyncClient) -> None:
    response = await create(client)

    assert response.status_code == status.HTTP_202_ACCEPTED
    data = response.json()
    assert data.keys() == {"payment_id", "status", "created_at"}
    assert data["status"] == "pending"

    async with SessionFactory() as session:
        payment = (await session.scalars(select(Payment))).one()
        event = (await session.scalars(select(OutboxEvent))).one()
    assert str(payment.id) == data["payment_id"]
    assert payment.idempotency_key == "key-1"
    assert payment.metadata_ == {"order_id": 42}
    assert payment.processed_at is None
    assert event.event_type == "payment.created"
    assert event.payload == {"payment_id": data["payment_id"]}
    assert event.published_at is None


@pytest.mark.parametrize("amount", [100.1, "100.1"])
async def test_amount_accepts_number_and_string(client: AsyncClient, amount: Any) -> None:
    payment_id = (await create(client, amount=amount)).json()["payment_id"]

    response = await client.get(f"{URL}/{payment_id}")

    assert response.json()["amount"] == "100.10"


async def test_get_payment_returns_all_fields(client: AsyncClient) -> None:
    created = (await create(client)).json()

    response = await client.get(f"{URL}/{created['payment_id']}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "payment_id": created["payment_id"],
        "status": "pending",
        "created_at": created["created_at"],
        "amount": "100.00",
        "currency": "RUB",
        "description": "Order 42",
        "metadata": {"order_id": 42},
        "idempotency_key": "key-1",
        "webhook_url": "https://example.com/hook",
        "processed_at": None,
    }


async def test_get_unknown_payment_is_not_found(client: AsyncClient) -> None:
    response = await client.get(f"{URL}/{uuid.uuid4()}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_get_payment_with_invalid_id_is_rejected(client: AsyncClient) -> None:
    response = await client.get(f"{URL}/not-a-uuid")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
@pytest.mark.parametrize(("method", "path"), [("POST", URL), ("GET", f"{URL}/{uuid.uuid4()}")])
async def test_request_without_valid_api_key_is_rejected(
    client: AsyncClient, method: str, path: str, headers: dict[str, str]
) -> None:
    client.headers.pop("X-API-Key")

    response = await client.request(method, path, headers=headers, json={})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert await count(Payment) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "0"},
        {"amount": "-5"},
        {"amount": "10.001"},
        {"currency": "GBP"},
        {"webhook_url": "not-a-url"},
        {"webhook_url": "ftp://example.com/hook"},
        {"description": "x" * 501},
        {"unexpected": 1},
        {"metadata": {"note": "x" * PaymentCreate.METADATA_MAX_BYTES}},
    ],
)
async def test_create_payment_with_invalid_body_is_rejected(
    client: AsyncClient, overrides: dict[str, Any]
) -> None:
    response = await create(client, **overrides)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert await count(Payment) == 0


async def test_create_payment_without_idempotency_key_is_rejected(client: AsyncClient) -> None:
    response = await client.post(URL, json=body())

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert await count(Payment) == 0


async def test_repeated_request_returns_same_payment(client: AsyncClient) -> None:
    first = await create(client)
    second = await create(client)

    assert second.status_code == status.HTTP_202_ACCEPTED
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert await count(Payment) == 1
    assert await count(OutboxEvent) == 1


async def test_repeated_request_returns_current_status(client: AsyncClient) -> None:
    payment_id = (await create(client)).json()["payment_id"]
    async with SessionFactory() as session:
        payment = await session.get(Payment, uuid.UUID(payment_id))
        assert payment
        payment.status = PaymentStatus.SUCCEEDED
        await session.commit()

    response = await create(client)

    assert response.json()["status"] == "succeeded"


async def test_equivalent_request_counts_as_repeat(client: AsyncClient) -> None:
    first = await create(
        client,
        metadata={"a": 1, "b": 2},
        webhook_url="https://example.com/hook",
    )
    second = await create(
        client,
        amount="100",
        metadata={"b": 2, "a": 1},
        webhook_url="HTTPS://Example.com:443/hook",
    )

    assert second.status_code == status.HTTP_202_ACCEPTED
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert await count(Payment) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "100.01"},
        {"currency": "USD"},
        {"description": "Other"},
        {"description": None},
        {"metadata": {}},
        {"webhook_url": "https://example.com/other"},
    ],
)
async def test_same_key_different_request_rejected(
    client: AsyncClient, overrides: dict[str, Any]
) -> None:
    await create(client)

    response = await create(client, **overrides)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "Idempotency-Key" in response.json()["detail"]
    assert await count(Payment) == 1
    assert await count(OutboxEvent) == 1


async def test_concurrent_requests_create_one_payment(client: AsyncClient) -> None:
    responses = await asyncio.gather(*(create(client) for _ in range(10)))

    assert {response.status_code for response in responses} == {status.HTTP_202_ACCEPTED}
    assert len({response.json()["payment_id"] for response in responses}) == 1
    assert await count(Payment) == 1
    assert await count(OutboxEvent) == 1


async def test_request_waits_for_uncommitted_duplicate(client: AsyncClient) -> None:
    async with SessionFactory() as holder:
        payment = Payment(
            idempotency_key="key-1",
            amount=Decimal("100.00"),
            currency=Currency.RUB,
            description="Order 42",
            metadata_={"order_id": 42},
            webhook_url="https://example.com/hook",
        )
        holder.add(payment)
        await holder.flush()

        request = asyncio.create_task(create(client))
        await asyncio.sleep(0.2)
        assert not request.done(), "the insert should be blocked on the unique index"
        await holder.commit()

    response = await request

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json()["payment_id"] == str(payment.id)
    assert await count(Payment) == 1
    assert await count(OutboxEvent) == 0


async def test_metadata_up_to_the_limit_is_accepted(client: AsyncClient) -> None:
    note = "я" * ((PaymentCreate.METADATA_MAX_BYTES - 12) // 2)

    response = await create(client, metadata={"note": note})

    assert response.status_code == status.HTTP_202_ACCEPTED
