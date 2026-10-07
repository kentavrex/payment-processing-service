import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from aiormq.exceptions import PublishError
from sqlalchemy import select

from app.config import settings
from app.database.models import OutboxEvent
from app.database.session import SessionFactory
from app.messaging import relay
from app.messaging.broker import DEAD_LETTER_QUEUE, NEW_QUEUE, RETRY_QUEUES, declare_topology
from tests.conftest import drain


@pytest_asyncio.fixture(autouse=True)
async def topology(relay_broker: None) -> None:
    await declare_topology(relay.broker)
    for queue in (NEW_QUEUE, DEAD_LETTER_QUEUE, *RETRY_QUEUES):
        await drain(relay.broker, queue)


async def add_events(count: int) -> list[OutboxEvent]:
    async with SessionFactory() as session:
        events = [
            OutboxEvent(event_type="payment.created", payload={"payment_id": str(uuid.uuid4())})
            for _ in range(count)
        ]
        session.add_all(events)
        await session.commit()
    return events


async def unpublished() -> int:
    async with SessionFactory() as session:
        rows = await session.scalars(select(OutboxEvent).where(OutboxEvent.published_at.is_(None)))
        return len(rows.all())


async def test_publishes_events_and_marks_them() -> None:
    events = await add_events(3)

    assert await relay.publish_pending() == 3

    messages = await drain(relay.broker, NEW_QUEUE)
    assert {message.message_id for message in messages} == {str(event.id) for event in events}
    assert {message.type for message in messages} == {"payment.created"}
    assert await unpublished() == 0


async def test_message_carries_payload_as_json() -> None:
    (event,) = await add_events(1)

    await relay.publish_pending()

    (message,) = await drain(relay.broker, NEW_QUEUE)
    assert message.content_type == "application/json"
    assert json.loads(message.body) == event.payload
    assert message.delivery_mode == 2


async def test_published_events_are_not_sent_again() -> None:
    await add_events(2)
    await relay.publish_pending()
    await drain(relay.broker, NEW_QUEUE)

    assert await relay.publish_pending() == 0

    assert await drain(relay.broker, NEW_QUEUE) == []


async def test_empty_outbox_publishes_nothing() -> None:
    assert await relay.publish_pending() == 0


async def test_concurrent_relays_never_publish_an_event_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "outbox_batch_size", 5)
    await add_events(20)

    counts = await asyncio.gather(relay.publish_pending(), relay.publish_pending())

    message_ids = [message.message_id for message in await drain(relay.broker, NEW_QUEUE)]
    assert len(message_ids) == sum(counts)
    assert len(set(message_ids)) == len(message_ids)
    assert await unpublished() == 20 - sum(counts)


async def test_failed_publish_keeps_events_unpublished() -> None:
    await add_events(2)
    await (await relay.broker.declare_queue(NEW_QUEUE)).delete()

    with pytest.raises(PublishError, match="NO_ROUTE"):
        await relay.publish_pending()

    assert await unpublished() == 2
