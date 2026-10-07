import logging

from fastapi import status
from httpx import AsyncClient

from app.main import HealthCheckAccessLogFilter, app


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", path, "1.1", 200),
        None,
    )


def test_health_checks_stay_out_of_the_access_log() -> None:
    access_log_filter = HealthCheckAccessLogFilter()

    assert access_log_filter.filter(access_record("/health")) is False
    assert access_log_filter.filter(access_record("/api/v1/payments")) is True


def test_openapi_documents_error_responses() -> None:
    paths = app.openapi()["paths"]

    assert paths["/api/v1/payments"]["post"]["responses"].keys() >= {"202", "401", "422"}
    assert paths["/api/v1/payments"]["post"]["tags"] == ["payments"]
    assert "demo-key" in app.openapi()["info"]["description"]
    assert paths["/api/v1/payments/{payment_id}"]["get"]["responses"].keys() >= {
        "200",
        "401",
        "404",
    }
