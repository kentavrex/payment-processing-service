import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.database.session import engine

HEALTH_PATH = "/health"

DESCRIPTION = """
Asynchronous payment processing: create a payment, it is processed in the background,
and the result is sent to `webhook_url`.

Authorize with `demo-key` on the demo server or `dev-api-key` locally.
[Source code and docs](https://github.com/kentavrex/payment-processing-service)
"""


class HealthCheckAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        return not (
            isinstance(arguments, tuple) and len(arguments) > 2 and arguments[2] == HEALTH_PATH
        )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


logging.getLogger("uvicorn.access").addFilter(HealthCheckAccessLogFilter())

app = FastAPI(title="Payments service", description=DESCRIPTION, lifespan=lifespan)
app.include_router(router)


@app.get(HEALTH_PATH, tags=["health"], summary="Liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok"}
