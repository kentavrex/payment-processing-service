import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi import status

from app.config import settings
from app.database.models import Currency, Payment, PaymentStatus
from app.database.session import SessionFactory
from app.services import processing
from app.services.processing import (
    PaymentNotFound,
    WebhookDestinationForbidden,
    WebhookFailed,
    process_payment,
)
from tests.conftest import webhook_client


async def add_payment(
    status: PaymentStatus = PaymentStatus.PENDING, webhook_url: str = "https://example.com/hook"
) -> uuid.UUID:
    async with SessionFactory() as session:
        payment = Payment(
            idempotency_key=str(uuid.uuid4()),
            amount=Decimal("100.00"),
            currency=Currency.RUB,
            description="Order 42",
            metadata_={"order_id": 42},
            webhook_url=webhook_url,
            status=status,
        )
        session.add(payment)
        await session.commit()
        return payment.id


async def load(payment_id: uuid.UUID) -> Payment:
    async with SessionFactory() as session:
        payment = await session.get(Payment, payment_id)
        assert payment
        return payment


def gateway(monkeypatch: pytest.MonkeyPatch, result: bool) -> list[uuid.UUID]:
    charged: list[uuid.UUID] = []

    async def charge(payment: Payment) -> bool:
        charged.append(payment.id)
        return result

    monkeypatch.setattr(processing, "charge", charge)
    return charged


@pytest.mark.parametrize(
    ("approved", "expected"), [(True, PaymentStatus.SUCCEEDED), (False, PaymentStatus.FAILED)]
)
async def test_gateway_result_becomes_status_and_webhook(
    monkeypatch: pytest.MonkeyPatch, approved: bool, expected: PaymentStatus
) -> None:
    gateway(monkeypatch, approved)
    payment_id = await add_payment()
    http_client, calls = webhook_client()

    await process_payment(payment_id, http_client)

    payment = await load(payment_id)
    assert payment.status == expected
    assert payment.processed_at is not None
    (body,) = calls
    assert body["payment_id"] == str(payment_id)
    assert body["status"] == expected
    assert body["processed_at"] is not None
    assert body["metadata"] == {"order_id": 42}


@pytest.mark.parametrize("final", [PaymentStatus.SUCCEEDED, PaymentStatus.FAILED])
async def test_final_payment_skips_gateway_and_resends_webhook(
    monkeypatch: pytest.MonkeyPatch, final: PaymentStatus
) -> None:
    charged = gateway(monkeypatch, True)
    payment_id = await add_payment(final)
    http_client, calls = webhook_client()

    await process_payment(payment_id, http_client)

    assert charged == []
    assert [call["status"] for call in calls] == [final]
    assert (await load(payment_id)).status == final


@pytest.mark.parametrize(
    ("respond", "error"),
    [
        (status.HTTP_500_INTERNAL_SERVER_ERROR, WebhookFailed),
        (status.HTTP_404_NOT_FOUND, WebhookFailed),
        (httpx.ConnectTimeout("timeout"), httpx.ConnectTimeout),
        (httpx.ConnectError("refused"), httpx.ConnectError),
    ],
)
async def test_failed_webhook_raises_but_keeps_the_status(
    monkeypatch: pytest.MonkeyPatch, respond: int | Exception, error: type[Exception]
) -> None:
    gateway(monkeypatch, True)
    payment_id = await add_payment()
    http_client, _ = webhook_client(respond)

    with pytest.raises(error):
        await process_payment(payment_id, http_client)

    assert (await load(payment_id)).status == PaymentStatus.SUCCEEDED


async def test_processing_unknown_payment_raises() -> None:
    http_client, calls = webhook_client()

    with pytest.raises(PaymentNotFound):
        await process_payment(uuid.uuid4(), http_client)

    assert calls == []


async def test_concurrent_duplicates_report_the_stored_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = 0
    both_inside = asyncio.Event()

    async def charge(payment: Payment) -> bool:
        nonlocal entered
        entered += 1
        position = entered
        if entered == 2:
            both_inside.set()
        await both_inside.wait()
        if position == 2:
            await asyncio.sleep(0.1)
        return position == 1

    monkeypatch.setattr(processing, "charge", charge)
    payment_id = await add_payment()
    http_client, calls = webhook_client()

    await asyncio.gather(
        process_payment(payment_id, http_client), process_payment(payment_id, http_client)
    )

    assert (await load(payment_id)).status == PaymentStatus.SUCCEEDED
    assert [call["status"] for call in calls] == ["succeeded", "succeeded"]


@pytest.mark.parametrize(
    "webhook_url",
    [
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://100.64.0.1/hook",
        "http://[::1]/hook",
    ],
)
async def test_webhook_to_internal_address_is_forbidden(
    monkeypatch: pytest.MonkeyPatch, webhook_url: str
) -> None:
    monkeypatch.setattr(settings, "webhook_allow_private_networks", False)
    gateway(monkeypatch, True)
    payment_id = await add_payment(webhook_url=webhook_url)
    http_client, calls = webhook_client()

    with pytest.raises(WebhookDestinationForbidden):
        await process_payment(payment_id, http_client)

    assert calls == []


async def test_webhook_to_public_address_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_allow_private_networks", False)
    gateway(monkeypatch, True)
    payment_id = await add_payment(webhook_url="http://93.184.216.34/hook")
    http_client, calls = webhook_client()

    await process_payment(payment_id, http_client)

    assert len(calls) == 1
