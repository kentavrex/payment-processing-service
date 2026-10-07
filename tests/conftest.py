import os

os.environ["API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://payments:payments@localhost:5432/payments_test"
)
os.environ["RABBITMQ_URL"] = os.environ.get(
    "TEST_RABBITMQ_URL", "amqp://payments:payments@localhost:5672/payments_test"
)
os.environ["GATEWAY_MIN_DELAY"] = "0.01"
os.environ["GATEWAY_MAX_DELAY"] = "0.02"
os.environ["RETRY_BASE_DELAY"] = "0.05"
os.environ["OUTBOX_POLL_INTERVAL"] = "0.05"
os.environ["WEBHOOK_ALLOW_PRIVATE_NETWORKS"] = "true"

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from aio_pika.abc import AbstractIncomingMessage
from alembic import command
from alembic.config import Config
from fastapi import status
from faststream.rabbit import RabbitBroker, RabbitQueue
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database.session import engine
from app.main import app
from app.messaging import relay


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    return Config(str(Path(__file__).parent.parent / "alembic.ini"))


@pytest_asyncio.fixture(scope="session", autouse=True)
async def database(alembic_config: Config) -> None:
    url = make_url(settings.database_url)
    assert url.database
    assert url.database.endswith("_test"), "refusing to drop a non-test database"

    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()

    await asyncio.to_thread(command.upgrade, alembic_config, "head")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def vhost() -> None:
    url = urlsplit(settings.rabbitmq_url)
    name = url.path.lstrip("/")
    assert name.endswith("_test"), "refusing to touch a non-test vhost"

    async with httpx.AsyncClient(
        base_url=f"http://{url.hostname}:15672/api", auth=(url.username or "", url.password or "")
    ) as management_api:
        (await management_api.put(f"/vhosts/{name}")).raise_for_status()
        permissions = {"configure": ".*", "write": ".*", "read": ".*"}
        (
            await management_api.put(f"/permissions/{name}/{url.username}", json=permissions)
        ).raise_for_status()


@pytest_asyncio.fixture(scope="session")
async def relay_broker(vhost: None) -> AsyncIterator[None]:
    await relay.broker.start()
    yield
    await relay.broker.stop()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(database: None) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE payments, outbox"))


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as test_client:
        yield test_client


async def wait_for(predicate: Callable[[], Awaitable[bool]], within: float = 5) -> None:
    deadline = time.monotonic() + within
    while not await predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


async def drain(broker: RabbitBroker, queue: RabbitQueue) -> list:
    declared = await broker.declare_queue(queue)
    messages = []
    while message := await declared.get(fail=False):
        await message.ack()
        messages.append(message)
    return messages


async def receive(
    broker: RabbitBroker, queue: RabbitQueue, within: float = 5
) -> AbstractIncomingMessage:
    declared = await broker.declare_queue(queue)
    deadline = time.monotonic() + within
    while not (message := await declared.get(fail=False)):
        assert time.monotonic() < deadline, f"no message in {queue.name}"
        await asyncio.sleep(0.02)
    await message.ack()
    return message


def webhook_client(
    respond: int | Exception | list[int | Exception] = status.HTTP_200_OK,
) -> tuple[httpx.AsyncClient, list[dict]]:
    calls: list[dict] = []
    plan = respond if isinstance(respond, list) else [respond]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        outcome = plan[min(len(calls), len(plan)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls
