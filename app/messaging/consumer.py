import asyncio
import logging
import uuid

import httpx
from faststream import AckPolicy, FastStream
from faststream.rabbit import Channel, RabbitMessage
from pydantic import BaseModel

from app.config import settings
from app.database.session import engine
from app.messaging.broker import NEW_QUEUE, PAYMENTS_EXCHANGE, declare_topology, new_broker
from app.services.processing import process_payment

logger = logging.getLogger("consumer")

broker = new_broker()
http_client = httpx.AsyncClient(timeout=settings.webhook_timeout)


class PaymentEvent(BaseModel):
    payment_id: uuid.UUID


@broker.subscriber(
    NEW_QUEUE,
    PAYMENTS_EXCHANGE,
    channel=Channel(prefetch_count=settings.consumer_prefetch),
    ack_policy=AckPolicy.REJECT_ON_ERROR,
)
async def handle_payment(event: PaymentEvent, message: RabbitMessage) -> None:
    attempt = int(message.headers.get("x-attempt", 1))
    try:
        await process_payment(event.payment_id, http_client)
    except Exception:
        if attempt >= settings.max_attempts:
            logger.exception(
                "payment %s failed %s times, dead-lettering", event.payment_id, attempt
            )
            raise
        delay = settings.retry_base_delay * 2 ** (attempt - 1)
        logger.warning(
            "payment %s attempt %s failed, retry in %ss",
            event.payment_id,
            attempt,
            delay,
            exc_info=True,
        )
        await broker.publish(
            message.body,
            exchange=PAYMENTS_EXCHANGE,
            routing_key=f"payments.new.retry.{attempt}",
            headers={"x-attempt": attempt + 1},
            expiration=delay,
            content_type=message.content_type,
            message_type=message.raw_message.type,
            message_id=message.message_id,
            persist=True,
        )


async def setup() -> None:
    await broker.connect()
    await declare_topology(broker)


async def shutdown() -> None:
    await http_client.aclose()
    await engine.dispose()


app = FastStream(broker, on_startup=[setup], after_shutdown=[shutdown])


if __name__ == "__main__":
    asyncio.run(app.run())
