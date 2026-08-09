"""LLM via LiteLLM (OpenAI-compatible) — uso opcional na caçada."""

from app.infrastructure.llm.client import chat_completion, score_company_candidate

__all__ = ["chat_completion", "score_company_candidate"]

# re-export for scripts
