"""Implementação de todos os nodes do pipeline de prospecção."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import SearchContext
from app.graph.state import CampaignGraphState
from app.infrastructure.crm.activity_service import ActivityService
from app.infrastructure.crm.client import CRMClient
from app.infrastructure.crm.company_service import CompanyService
from app.infrastructure.crm.contact_service import ContactService
from app.infrastructure.crm.lead_service import LeadService
from app.infrastructure.crm.opportunity_service import OpportunityService
from app.infrastructure.email.smtp import SMTPService
from app.infrastructure.email.templates import TemplateSelector
from app.providers.registry import get_provider_registry

logger = get_logger(__name__)


def _log(msg: str) -> list[str]:
    logger.info(msg)
    return [f"{datetime.now(timezone.utc).isoformat()} | {msg}"]


def _check_control(state: CampaignGraphState) -> dict[str, Any] | None:
    if state.get("cancelled"):
        return {"status": "cancelled", "logs": _log("Campanha cancelada")}
    if state.get("paused"):
        return {"status": "paused", "logs": _log("Campanha pausada")}
    return None


async def create_campaign_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    return {
        "status": "running",
        "companies": [],
        "contacts": [],
        "current_index": 0,
        "logs": _log(f"CreateCampaign: {state.get('campaign_id')}"),
    }


async def select_provider_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    registry = get_provider_registry()
    niche = state.get("niche") or state.get("provider") or ""
    provider = registry.resolve(niche)
    return {
        "provider": provider.code,
        "niche": provider.niche,
        "logs": _log(f"SelectProvider: {provider.code} ({provider.niche})"),
    }


async def search_companies_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    registry = get_provider_registry()
    provider = registry.resolve(state.get("provider") or state.get("niche") or "")
    ctx = SearchContext(
        query=state.get("query") or "",
        city=state.get("city") or "",
        state=state.get("state") or "",
        max_results=int(state.get("max_results") or 20),
    )
    results = await provider.search_companies(ctx)
    results = [r for r in results if r.is_valid_company()]
    companies = [r.model_dump() for r in results]
    return {
        "companies": companies,
        "status": "searching",
        "logs": _log(f"SearchCompanies: {len(companies)} encontradas via {provider.code}"),
    }


async def normalize_companies_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    companies = state.get("companies") or []
    normalized: list[dict[str, Any]] = []
    for raw in companies:
        pr = ProviderResult(**{k: raw.get(k, "") for k in ProviderResult.model_fields if k != "extra"})
        pr.extra = raw.get("extra") or {}
        pr.company_name = re.sub(r"\s+", " ", (pr.company_name or "").strip())
        pr.website = (pr.website or "").strip().rstrip("/")
        pr.email = (pr.email or "").strip().lower()
        pr.phone = re.sub(r"[^\d+\s()-]", "", pr.phone or "").strip()
        pr.city = (pr.city or state.get("city") or "").strip()
        pr.state = (pr.state or state.get("state") or "").strip()
        pr.segment = pr.segment or state.get("niche") or ""
        if pr.is_valid_company():
            normalized.append(pr.model_dump())
    return {
        "companies": normalized,
        "logs": _log(f"NormalizeCompanies: {len(normalized)} válidas"),
    }


async def remove_duplicates_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    companies = state.get("companies") or []
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for raw in companies:
        pr = ProviderResult(**{k: raw.get(k, "") for k in ProviderResult.model_fields if k != "extra"})
        pr.extra = raw.get("extra") or {}
        key = pr.normalize_key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(pr.model_dump())
    return {
        "companies": unique,
        "current_index": 0,
        "logs": _log(f"RemoveDuplicates: {len(unique)} únicas (de {len(companies)})"),
    }


async def find_contacts_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    companies = state.get("companies") or []
    if not companies:
        return {"contacts": [], "logs": _log("FindContacts: nenhuma empresa")}

    registry = get_provider_registry()
    provider = registry.resolve(state.get("provider") or state.get("niche") or "")
    ctx = SearchContext(
        query=state.get("query") or "",
        city=state.get("city") or "",
        state=state.get("state") or "",
        max_results=int(state.get("max_results") or 20),
    )
    contacts: list[dict[str, Any]] = []
    for raw in companies:
        company = ProviderResult(**{k: raw.get(k, "") for k in ProviderResult.model_fields if k != "extra"})
        company.extra = raw.get("extra") or {}
        found = await provider.find_contacts(company, ctx)
        contacts.extend(c.model_dump() for c in found)

    current = contacts[0] if contacts else {}
    return {
        "contacts": contacts,
        "company": current,
        "contact": current,
        "current_index": 0,
        "logs": _log(f"FindContacts: {len(contacts)} contatos"),
    }


async def find_emails_node(state: CampaignGraphState) -> dict[str, Any]:
    """Busca e-mail (1ª + 2ª passagem). Sem e-mail → remove da lista (descarta)."""
    control = _check_control(state)
    if control:
        return control

    from app.providers.email_enrichment import require_email

    contacts = state.get("contacts") or []
    if not contacts:
        # tenta a partir de companies
        contacts = state.get("companies") or []
    if not contacts:
        return {
            "contacts": [],
            "contact": {},
            "status": "rejected",
            "logs": _log("FindEmails: nenhum contato/empresa"),
        }

    kept: list[dict[str, Any]] = []
    discarded = 0
    for raw in contacts:
        contact = ProviderResult(
            **{k: raw.get(k, "") for k in ProviderResult.model_fields if k != "extra"}
        )
        contact.extra = raw.get("extra") or {}
        result = await require_email(contact, deep=True)
        if result is None:
            discarded += 1
            continue
        kept.append(result.model_dump())

    current = kept[0] if kept else {}
    return {
        "contacts": kept,
        "contact": current,
        "company": current,
        "status": "email_found" if kept else "rejected",
        "logs": _log(
            f"FindEmails: mantidos={len(kept)} descartados_sem_email={discarded}"
        ),
    }


async def validate_lead_node(state: CampaignGraphState) -> dict[str, Any]:
    """E-mail é obrigatório. Sem e-mail → rejected (descarta)."""
    control = _check_control(state)
    if control:
        return control

    from app.providers.email_enrichment import has_valid_email

    # Prefere lista filtrada; se vazia, rejeita campanha de item
    contacts = state.get("contacts") or []
    with_email = [
        c for c in contacts if has_valid_email(str(c.get("email") or ""))
    ]
    contact = with_email[0] if with_email else (state.get("contact") or {})

    if not has_valid_email(str(contact.get("email") or "")):
        return {
            "contacts": [],
            "contact": {},
            "qualification_score": 0,
            "qualification_notes": "sem e-mail — descartado",
            "status": "rejected",
            "logs": _log("ValidateLead: REJECTED (e-mail obrigatório, não encontrado)"),
        }

    score = 50  # base por ter e-mail
    notes = ["tem e-mail"]
    if contact.get("company_name"):
        score += 20
        notes.append("tem empresa")
    if contact.get("phone"):
        score += 10
        notes.append("tem telefone")
    if contact.get("website"):
        score += 10
        notes.append("tem website")
    if contact.get("contact_name"):
        score += 10
        notes.append("tem contato")

    return {
        "contacts": with_email,
        "contact": contact,
        "company": contact,
        "qualification_score": score,
        "qualification_notes": ", ".join(notes),
        "status": "qualified",
        "logs": _log(f"ValidateLead: score={score} status=qualified email={contact.get('email')}"),
    }


async def create_crm_company_node(state: CampaignGraphState) -> dict[str, Any]:
    """Sync completo Account+Contact+Lead via CRMSyncService (API.md)."""
    control = _check_control(state)
    if control:
        return control
    if state.get("status") == "rejected":
        return {"logs": _log("CreateCRMCompany: pulado (lead rejeitado)")}

    from app.infrastructure.crm.sync import CRMSyncService

    contact = state.get("contact") or state.get("company") or {}
    email = contact.get("email") or ""
    if not email:
        return {
            "status": "rejected",
            "logs": _log("CRM: sem e-mail — não cria registros"),
        }

    try:
        sync = CRMSyncService()
        result = await sync.sync_prospect(
            company_name=contact.get("company_name") or "Empresa",
            contact_name=contact.get("contact_name") or contact.get("company_name") or "Contato",
            email=email,
            phone=contact.get("phone") or "",
            website=contact.get("website") or "",
            city=contact.get("city") or state.get("city") or "",
            state=contact.get("state") or state.get("state") or "",
            niche=state.get("niche") or "",
            description=f"Campanha {state.get('campaign_id')} | score={state.get('qualification_score')}",
        )
        return {
            "crm_company_id": result.account_id,
            "crm_contact_id": result.contact_id,
            "crm_lead_id": result.lead_id,
            "status": "crm_created" if result.ok else state.get("status"),
            "logs": _log(
                f"CRM sync account={result.account_id} contact={result.contact_id} "
                f"lead={result.lead_id} errors={result.errors}"
            ),
        }
    except Exception as exc:
        logger.warning("crm_sync_failed", error=str(exc))
        return {
            "crm_company_id": None,
            "crm_contact_id": None,
            "crm_lead_id": None,
            "logs": _log(f"CRM sync falhou: {exc}"),
        }


async def create_crm_contact_node(state: CampaignGraphState) -> dict[str, Any]:
    """Já sincronizado em CreateCRMCompany (CRMSyncService). No-op se IDs presentes."""
    control = _check_control(state)
    if control:
        return control
    if state.get("crm_contact_id"):
        return {"logs": _log(f"CreateCRMContact: já existe {state.get('crm_contact_id')}")}
    if state.get("status") == "rejected":
        return {"logs": _log("CreateCRMContact: pulado")}
    # fallback se company node não rodou sync completo
    return await create_crm_company_node(state)


async def create_crm_lead_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    if state.get("crm_lead_id"):
        return {
            "status": "crm_created",
            "logs": _log(f"CreateCRMLead: já existe {state.get('crm_lead_id')}"),
        }
    if state.get("status") == "rejected":
        return {"logs": _log("CreateCRMLead: pulado")}
    return await create_crm_company_node(state)


async def select_email_template_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    if state.get("status") == "rejected":
        return {"logs": _log("SelectEmailTemplate: pulado (lead rejeitado)")}

    niche = state.get("niche") or ""
    selector = TemplateSelector()
    filename, _html, content_hash = selector.load(niche)
    return {
        "template_name": filename,
        "logs": _log(f"SelectEmailTemplate: {filename} hash={content_hash[:12]}"),
    }


async def send_email_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    if state.get("status") == "rejected":
        return {"email_status": "skipped", "logs": _log("SendEmail: pulado (lead rejeitado)")}

    # Prospectar sem disparar e-mail (config.skip_email ou flag no state)
    if state.get("skip_email"):
        return {
            "email_status": "skipped",
            "status": "qualified" if state.get("status") != "rejected" else state.get("status"),
            "logs": _log("SendEmail: skip_email=true (sem envio)"),
        }

    contact = state.get("contact") or {}
    to = contact.get("email") or ""
    if not to:
        return {"email_status": "skipped", "logs": _log("SendEmail: sem e-mail")}

    niche = state.get("niche") or ""
    selector = TemplateSelector()
    filename, html, _ = selector.load(niche)
    subject = selector.subject_for(
        niche, seed=str(state.get("campaign_item_id") or state.get("thread_id") or to)
    )

    smtp = SMTPService()
    result = await smtp.send_html(to=to, subject=subject, html_body=html)
    return {
        "template_name": filename,
        "email_status": result.get("status"),
        "status": "email_sent" if result.get("status") in {"sent", "dry_run"} else "failed",
        "logs": _log(f"SendEmail: {result.get('status')} -> {to} template={filename}"),
    }


async def register_activity_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    if state.get("status") == "rejected":
        return {"logs": _log("RegisterActivity: pulado")}

    lead_id = state.get("crm_lead_id") or ""
    contact = state.get("contact") or {}
    try:
        client = CRMClient()
        svc = ActivityService(client)
        if lead_id:
            await svc.log_email_sent(
                subject=f"Prospecção {state.get('niche')}",
                to=contact.get("email") or "",
                parent_type="Lead",
                parent_id=lead_id,
            )
        return {"logs": _log(f"RegisterActivity: lead={lead_id or 'n/a'}")}
    except Exception as exc:
        return {"logs": _log(f"RegisterActivity: falha (continua) {exc}")}


async def wait_response_node(state: CampaignGraphState) -> dict[str, Any]:
    """Marca item como aguardando resposta (polling assíncrono externo)."""
    control = _check_control(state)
    if control:
        return control
    if state.get("status") == "rejected":
        return {"logs": _log("WaitResponse: pulado")}
    return {
        "status": "waiting_response",
        "logs": _log("WaitResponse: item marcado como aguardando resposta"),
    }


async def update_pipeline_node(state: CampaignGraphState) -> dict[str, Any]:
    control = _check_control(state)
    if control:
        return control
    if state.get("status") == "rejected":
        return {"logs": _log("UpdatePipeline: pulado")}

    company = state.get("company") or state.get("contact") or {}
    try:
        client = CRMClient()
        opp = OpportunityService(client)
        result = await opp.create(
            name=f"Prospecção — {company.get('company_name') or 'Lead'}",
            account_id=state.get("crm_company_id") or "",
            stage=OpportunityService.STAGE_PROSPECTING,
            description=f"Campanha {state.get('campaign_id')}",
            lead_source="Partner",
        )
        return {
            "crm_opportunity_id": result.get("id"),
            "status": "pipeline_updated",
            "logs": _log(f"UpdatePipeline: opportunity={result.get('id')}"),
        }
    except Exception as exc:
        return {
            "crm_opportunity_id": None,
            "status": "pipeline_updated",
            "logs": _log(f"UpdatePipeline: falha CRM (continua) {exc}"),
        }


async def finish_campaign_node(state: CampaignGraphState) -> dict[str, Any]:
    if state.get("cancelled"):
        return {"status": "cancelled", "logs": _log("FinishCampaign: cancelada")}
    if state.get("paused"):
        return {"status": "paused", "logs": _log("FinishCampaign: pausada")}
    final = state.get("status") or "completed"
    if final in {"running", "searching", "qualified", "crm_created", "email_sent", "waiting_response", "pipeline_updated"}:
        final = "completed"
    return {
        "status": final,
        "logs": _log(f"FinishCampaign: status={final}"),
    }
