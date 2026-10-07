import asyncio
import ipaddress
import random
import socket
import uuid

import httpx

from app.config import settings
from app.database import repository
from app.database.models import Payment, PaymentStatus
from app.database.session import SessionFactory
from app.schemas import PaymentRead


class PaymentNotFound(Exception):
    pass


class WebhookFailed(Exception):
    pass


class WebhookDestinationForbidden(WebhookFailed):
    pass


async def charge(payment: Payment) -> bool:
    await asyncio.sleep(random.uniform(settings.gateway_min_delay, settings.gateway_max_delay))
    return random.random() < settings.gateway_success_rate


async def ensure_public_destination(url: str) -> None:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(httpx.URL(url).host, None)
    except socket.gaierror:
        raise WebhookFailed("webhook host does not resolve") from None
    for *_, socket_address in addresses:
        address = ipaddress.ip_address(str(socket_address[0]).split("%")[0])
        if not address.is_global:
            raise WebhookDestinationForbidden("webhook destination is not a public address")


async def send_webhook(http_client: httpx.AsyncClient, payment: Payment) -> None:
    if not settings.webhook_allow_private_networks:
        await ensure_public_destination(payment.webhook_url)
    body = PaymentRead.model_validate(payment).model_dump(mode="json")
    response = await http_client.post(payment.webhook_url, json=body)
    if not response.is_success:
        raise WebhookFailed(f"webhook responded {response.status_code}")


async def process_payment(payment_id: uuid.UUID, http_client: httpx.AsyncClient) -> None:
    async with SessionFactory() as session:
        payment = await repository.get_payment(session, payment_id)
    if payment is None:
        raise PaymentNotFound(payment_id)

    if payment.status == PaymentStatus.PENDING:
        succeeded = await charge(payment)
        status = PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED
        async with SessionFactory() as session:
            stored = await repository.set_final_status(session, payment_id, status)
            await session.commit()
        payment = stored

    await send_webhook(http_client, payment)
