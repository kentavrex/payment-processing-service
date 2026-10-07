import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.dependencies import SessionDependency, verify_api_key
from app.schemas import ErrorResponse, PaymentAccepted, PaymentCreate, PaymentRead
from app.services import payments

router = APIRouter(
    prefix="/api/v1/payments",
    tags=["payments"],
    dependencies=[Depends(verify_api_key)],
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}},
)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PaymentAccepted,
    responses={status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse}},
    summary="Create a payment",
    description=(
        "Accepts the payment and answers `202` with status `pending` right away, "
        "processing happens asynchronously. Repeating the request with the same `Idempotency-Key` "
        "and body returns the same payment with its current status, a different body gives `422`."
    ),
)
async def create_payment(
    data: PaymentCreate,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
    session: SessionDependency,
):
    try:
        return await payments.create_payment(session, idempotency_key, data)
    except payments.IdempotencyKeyReused:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Idempotency-Key was already used with a different request",
        ) from None


@router.get(
    "/{payment_id}",
    response_model=PaymentRead,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    summary="Get a payment",
    description="Current state of the payment: status, processing time and all request fields.",
)
async def get_payment(payment_id: uuid.UUID, session: SessionDependency):
    payment = await payments.get_payment(session, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment
