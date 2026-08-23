"""LLM via LiteLLM (OpenAI-compatible) — uso opcional na caçada."""

from app.infrastructure.llm.client import (
    chat_completion,
    chat_completion_used,
    is_local_llm_model,
    salvage_saved_contact,
    score_company_candidate,
    score_email_belongs_to_business,
)

__all__ = [
    "chat_completion",
    "chat_completion_used",
    "is_local_llm_model",
    "salvage_saved_contact",
    "score_company_candidate",
    "score_email_belongs_to_business",
]

# re-export for scripts
