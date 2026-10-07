import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from aio_pika.abc import AbstractIncomingMessage
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.database.models import Payment, PaymentStatus
from app.database.session import SessionFactory
from app.messaging import consumer, relay
from app.messaging.broker import (
    DEAD_LETTER_QUEUE,
    NEW_QUEUE,
    NEW_ROUTING_KEY,
    PAYMENTS_EXCHANGE,
    RETRY_QUEUES,
)
from tests.conftest import drain, receive, wait_for, webhook_client

URL = "/api/v1/payments"


@pytest_asyncio.fixture(scope="module")
async def running_consumer(vhost: None) -> AsyncIterator[None]:
    await consumer.setup()
    await consumer.broker.start()
    yield
    await consumer.broker.stop()


@pytest_asyncio.fixture(autouse=True)
async def empty_queues(running_consumer: None) -> None:
    for queue in (NEW_QUEUE, DEAD_LETTER_QUEUE, *RETRY_QUEUES):
        await drain(consumer.broker, queue)


@pytest.fixture(autouse=True)
def reliable_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_success_rate", 1.0)


async def create_payment(client: AsyncClient, webhook_url: str = "https://client.test/hook") -> str:
    response = await client.post(
        URL,
        json={"amount": "10.00", "currency": "USD", "webhook_url": webhook_url},
        headers={"Idempotency-Key": "key-1"},
    )
    return response.json()["payment_id"]


def x_death(message: AbstractIncomingMessage) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], message.headers["x-death"])


async def status_of(payment_id: str) -> PaymentStatus:
    async with SessionFactory() as session:
        payments = await session.scalars(select(Payment).where(Payment.id == payment_id))
        return payments.one().status


async def test_payment_goes_from_api_to_webhook(
    client: AsyncClient, relay_broker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    http_client, calls = webhook_client()
    monkeypatch.setattr(consumer, "http_client", http_client)
    payment_id = await create_payment(client)

    await relay.publish_pending()

    async def delivered() -> bool:
        return bool(calls)

    await wait_for(delivered)
    assert calls[0]["payment_id"] == payment_id
    assert calls[0]["status"] == "succeeded"
    assert await status_of(payment_id) == PaymentStatus.SUCCEEDED


async def test_exhausted_retries_end_in_dead_letter_queue(
    client: AsyncClient, relay_broker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamps: list[float] = []

    def always_fail(request: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        return httpx.Response(status.HTTP_500_INTERNAL_SERVER_ERROR)

    monkeypatch.setattr(
        consumer, "http_client", httpx.AsyncClient(transport=httpx.MockTransport(always_fail))
    )
    await create_payment(client)

    await relay.publish_pending()
    dead = await receive(consumer.broker, DEAD_LETTER_QUEUE)

    assert len(stamps) == settings.max_attempts
    assert await drain(consumer.broker, NEW_QUEUE) == []
    base = settings.retry_base_delay
    assert stamps[1] - stamps[0] >= base
    assert stamps[2] - stamps[1] >= base * 2
    assert {(death["queue"], death["reason"]) for death in x_death(dead)} == {
        ("payments.new.retry.2", "expired"),
        ("payments.new", "rejected"),
    }


async def test_message_recovers_after_a_failed_attempt(
    client: AsyncClient, relay_broker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    http_client, calls = webhook_client([status.HTTP_500_INTERNAL_SERVER_ERROR, status.HTTP_200_OK])
    monkeypatch.setattr(consumer, "http_client", http_client)
    payment_id = await create_payment(client)

    await relay.publish_pending()

    async def delivered_twice() -> bool:
        return len(calls) == 2

    await wait_for(delivered_twice)
    await asyncio.sleep(0.2)
    assert [call["status"] for call in calls] == ["succeeded", "succeeded"]
    assert await drain(consumer.broker, DEAD_LETTER_QUEUE) == []
    assert await status_of(payment_id) == PaymentStatus.SUCCEEDED


@pytest.mark.parametrize("body", [b"not json", b'{"payment_id": "nope"}', b"{}"])
async def test_malformed_message_goes_straight_to_dead_letter_queue(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    http_client, calls = webhook_client()
    monkeypatch.setattr(consumer, "http_client", http_client)

    await consumer.broker.publish(
        body, exchange=PAYMENTS_EXCHANGE, routing_key=NEW_ROUTING_KEY, content_type="text/plain"
    )

    dead = await receive(consumer.broker, DEAD_LETTER_QUEUE)
    assert dead.body == body
    assert calls == []
    assert [death["reason"] for death in x_death(dead)] == ["rejected"]


async def test_retried_message_keeps_its_content(
    client: AsyncClient, relay_broker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    http_client, _ = webhook_client(status.HTTP_500_INTERNAL_SERVER_ERROR)
    monkeypatch.setattr(consumer, "http_client", http_client)
    payment_id = await create_payment(client)

    await relay.publish_pending()
    dead = await receive(consumer.broker, DEAD_LETTER_QUEUE)

    assert dead.content_type == "application/json"
    assert dead.type == "payment.created"
    assert payment_id in dead.body.decode()
    assert dead.headers["x-attempt"] == settings.max_attempts


async def test_failed_webhook_does_not_leak_its_url_to_logs(
    client: AsyncClient,
    relay_broker: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    http_client, _ = webhook_client(status.HTTP_500_INTERNAL_SERVER_ERROR)
    monkeypatch.setattr(consumer, "http_client", http_client)
    await create_payment(client, webhook_url="https://client.test/hook?token=s3cret")

    await relay.publish_pending()
    await receive(consumer.broker, DEAD_LETTER_QUEUE)

    assert "webhook responded 500" in caplog.text
    assert "s3cret" not in caplog.text
