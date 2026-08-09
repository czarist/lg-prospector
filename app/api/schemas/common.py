from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    total: int
    offset: int = 0
    limit: int = 50
    items: list[T]


class MessageResponse(BaseModel):
    message: str
    detail: str | None = None
