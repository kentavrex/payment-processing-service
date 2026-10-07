from faststream.rabbit import Channel, RabbitBroker, RabbitExchange, RabbitQueue

from app.config import settings


def new_broker() -> RabbitBroker:
    return RabbitBroker(settings.rabbitmq_url, default_channel=Channel(on_return_raises=True))


PAYMENTS_EXCHANGE = RabbitExchange("payments")
DEAD_LETTER_EXCHANGE = RabbitExchange("payments.dlx")

NEW_ROUTING_KEY = "payments.new"
DEAD_LETTER_ROUTING_KEY = "payments.new.dlq"

NEW_QUEUE = RabbitQueue(
    "payments.new",
    routing_key=NEW_ROUTING_KEY,
    arguments={
        "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE.name,
        "x-dead-letter-routing-key": DEAD_LETTER_ROUTING_KEY,
    },
)
DEAD_LETTER_QUEUE = RabbitQueue("payments.new.dlq", routing_key=DEAD_LETTER_ROUTING_KEY)

RETRY_QUEUES = [
    RabbitQueue(
        f"payments.new.retry.{attempt}",
        routing_key=f"payments.new.retry.{attempt}",
        arguments={
            "x-dead-letter-exchange": PAYMENTS_EXCHANGE.name,
            "x-dead-letter-routing-key": NEW_ROUTING_KEY,
        },
    )
    for attempt in range(1, settings.max_attempts)
]


async def declare_topology(broker: RabbitBroker) -> None:
    payments = await broker.declare_exchange(PAYMENTS_EXCHANGE)
    dlx = await broker.declare_exchange(DEAD_LETTER_EXCHANGE)
    for queue, exchange in [
        (NEW_QUEUE, payments),
        (DEAD_LETTER_QUEUE, dlx),
        *((q, payments) for q in RETRY_QUEUES),
    ]:
        declared = await broker.declare_queue(queue)
        await declared.bind(exchange, routing_key=queue.routing_key)
