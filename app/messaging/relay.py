import asyncio
import contextlib
import logging
import signal

from app.config import settings
from app.database import repository
from app.database.session import SessionFactory, engine
from app.messaging.broker import NEW_ROUTING_KEY, PAYMENTS_EXCHANGE, declare_topology, new_broker

logger = logging.getLogger("relay")
broker = new_broker()


async def publish_pending() -> int:
    async with SessionFactory() as session, session.begin():
        events = await repository.lock_unpublished_events(session, settings.outbox_batch_size)
        for event in events:
            await broker.publish(
                event.payload,
                exchange=PAYMENTS_EXCHANGE,
                routing_key=NEW_ROUTING_KEY,
                message_id=str(event.id),
                message_type=event.event_type,
                persist=True,
                timeout=5,
            )
            repository.mark_published(event)
    return len(events)


async def run(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            published = await publish_pending()
        except Exception:
            logger.exception("publishing outbox batch failed, retrying")
            published = 0
        if published:
            logger.info("published %s outbox events", published)
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.outbox_poll_interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(stop_signal, stop.set)

    await broker.start()
    try:
        await declare_topology(broker)
        await run(stop)
    finally:
        await broker.stop()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
