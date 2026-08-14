"""LLM via LiteLLM (OpenAI-compatible) — uso opcional na caçada."""

from app.infrastructure.llm.client import (
    chat_completion,
    score_company_candidate,
    score_email_belongs_to_business,
)

__all__ = [
    "chat_completion",
    "score_company_candidate",
    "score_email_belongs_to_business",
]

# re-export for scripts
