import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.database.models import Currency, PaymentStatus


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    METADATA_MAX_BYTES: ClassVar[int] = 16 * 1024

    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: Currency
    description: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    webhook_url: HttpUrl

    @field_validator("metadata")
    @classmethod
    def metadata_fits(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False).encode()) > cls.METADATA_MAX_BYTES:
            raise ValueError(f"metadata must be at most {cls.METADATA_MAX_BYTES} bytes as JSON")
        return value


class PaymentAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: uuid.UUID = Field(validation_alias="id")
    status: PaymentStatus
    created_at: datetime


class PaymentRead(PaymentAccepted):
    amount: Decimal
    currency: Currency
    description: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    idempotency_key: str
    webhook_url: str
    processed_at: datetime | None


class ErrorResponse(BaseModel):
    detail: str
