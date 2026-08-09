"""Montagem do grafo LangGraph de campanha."""

from __future__ import annotations

from typing import Any, Literal, Optional

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.pipeline import (
    create_campaign_node,
    create_crm_company_node,
    create_crm_contact_node,
    create_crm_lead_node,
    find_contacts_node,
    find_emails_node,
    finish_campaign_node,
    normalize_companies_node,
    register_activity_node,
    remove_duplicates_node,
    search_companies_node,
    select_email_template_node,
    select_provider_node,
    send_email_node,
    update_pipeline_node,
    validate_lead_node,
    wait_response_node,
)
from app.graph.state import CampaignGraphState

_compiled = None


def _should_continue_after_validate(
    state: CampaignGraphState,
) -> Literal["CreateCRMCompany", "FinishCampaign"]:
    if state.get("cancelled") or state.get("paused"):
        return "FinishCampaign"
    if state.get("status") == "rejected":
        return "FinishCampaign"
    # Se não há contatos/empresas, finaliza
    if not (state.get("contacts") or state.get("companies")):
        return "FinishCampaign"
    return "CreateCRMCompany"


def _control_gate(state: CampaignGraphState) -> Literal["continue", "finish"]:
    if state.get("cancelled") or state.get("paused"):
        return "finish"
    return "continue"


def build_campaign_graph() -> Any:
    """
    Fluxo:
    CreateCampaign → SelectProvider → SearchCompanies → NormalizeCompanies
    → RemoveDuplicates → FindContacts → FindEmails → ValidateLead
    → CreateCRMCompany → CreateCRMContact → CreateCRMLead
    → SelectEmailTemplate → SendEmail → RegisterActivity
    → WaitResponse → UpdatePipeline → FinishCampaign
    """
    graph = StateGraph(CampaignGraphState)

    graph.add_node("CreateCampaign", create_campaign_node)
    graph.add_node("SelectProvider", select_provider_node)
    graph.add_node("SearchCompanies", search_companies_node)
    graph.add_node("NormalizeCompanies", normalize_companies_node)
    graph.add_node("RemoveDuplicates", remove_duplicates_node)
    graph.add_node("FindContacts", find_contacts_node)
    graph.add_node("FindEmails", find_emails_node)
    graph.add_node("ValidateLead", validate_lead_node)
    graph.add_node("CreateCRMCompany", create_crm_company_node)
    graph.add_node("CreateCRMContact", create_crm_contact_node)
    graph.add_node("CreateCRMLead", create_crm_lead_node)
    graph.add_node("SelectEmailTemplate", select_email_template_node)
    graph.add_node("SendEmail", send_email_node)
    graph.add_node("RegisterActivity", register_activity_node)
    graph.add_node("WaitResponse", wait_response_node)
    graph.add_node("UpdatePipeline", update_pipeline_node)
    graph.add_node("FinishCampaign", finish_campaign_node)

    graph.add_edge(START, "CreateCampaign")
    graph.add_edge("CreateCampaign", "SelectProvider")
    graph.add_edge("SelectProvider", "SearchCompanies")
    graph.add_edge("SearchCompanies", "NormalizeCompanies")
    graph.add_edge("NormalizeCompanies", "RemoveDuplicates")
    graph.add_edge("RemoveDuplicates", "FindContacts")
    graph.add_edge("FindContacts", "FindEmails")
    graph.add_edge("FindEmails", "ValidateLead")

    graph.add_conditional_edges(
        "ValidateLead",
        _should_continue_after_validate,
        {
            "CreateCRMCompany": "CreateCRMCompany",
            "FinishCampaign": "FinishCampaign",
        },
    )

    graph.add_edge("CreateCRMCompany", "CreateCRMContact")
    graph.add_edge("CreateCRMContact", "CreateCRMLead")
    graph.add_edge("CreateCRMLead", "SelectEmailTemplate")
    graph.add_edge("SelectEmailTemplate", "SendEmail")
    graph.add_edge("SendEmail", "RegisterActivity")
    graph.add_edge("RegisterActivity", "WaitResponse")
    graph.add_edge("WaitResponse", "UpdatePipeline")
    graph.add_edge("UpdatePipeline", "FinishCampaign")
    graph.add_edge("FinishCampaign", END)

    return graph.compile()


def get_campaign_graph() -> Any:
    global _compiled
    if _compiled is None:
        _compiled = build_campaign_graph()
    return _compiled
