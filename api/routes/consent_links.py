"""/v1/consent-link: the hosted consent screen's API (ADR-0008).

Public: the Consent link token is the only credential. It arrives in the `Consent-Link`
header, never in a path, so no access log records it, and only its hash is looked up.
A link works until a newer one is minted or its request is resolved or expires; any
other link is `CONSENT_LINK_INVALID`, whatever the reason, so a guessed or stale token
learns nothing. Opening the view doesn't use a link up; the first resolution does.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import AwareDatetime, BaseModel

from api.consent_view import mandate_inactive, money_text, reason_text
from api.deps import ClockDep, PackDep, SettingsDep, StoreDep, WorkflowDep, require_idempotency_key
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from api.routes.consent_requests import (
    ConsentResolutionOut,
    approve,
    decline,
    resolution_out,
    signal_resolved,
)
from core.domain.consent import ConsentLink, ConsentRequestId, ConsentStatus, hash_link_token
from core.domain.decisions import ReasonCode, decision_time
from core.domain.intents import IntentVerdict
from core.domain.money import Money
from core.policy.packs import load_pack
from core.ports import TenantTransaction

router = APIRouter(prefix="/v1/consent-link", tags=["consent link"])

LinkToken = Annotated[str | None, Header(alias="Consent-Link")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


class PaymentView(BaseModel):
    amount: Money
    amount_text: str
    merchant_name: str
    mcc: str
    agent_name: str | None


class ReasonView(BaseModel):
    code: ReasonCode
    text: str


class ConsentLinkView(BaseModel):
    consent_request_id: ConsentRequestId
    status: ConsentStatus
    expires_at: AwareDatetime
    payment: PaymentView
    reasons: list[ReasonView]
    intent_verdict: IntentVerdict | None
    mandate_inactive: bool


def _invalid() -> ApiError:
    return ApiError(
        404, "CONSENT_LINK_INVALID", "This consent link no longer works. Ask for a new one."
    )


def _link(store: StoreDep, token: str | None) -> ConsentLink:
    link = store.consent_link(hash_link_token(token)) if token else None
    if link is None or link.revoked_at is not None:
        raise _invalid()
    return link


def _still_open(tx: TenantTransaction, link: ConsentLink, now: datetime) -> None:
    """Re-check inside the transaction: not revoked since, and the request still pending."""
    current = tx.get_consent_link(link.token_hash)
    consent = tx.find_consent_request(link.consent_request_id)
    if current is None or current.revoked_at is not None:
        raise _invalid()
    if consent is None or consent.status(now) != "pending":
        raise _invalid()


@router.get("")
def view_consent_link(
    store: StoreDep, clock: ClockDep, settings: SettingsDep, token: LinkToken = None
) -> ConsentLinkView:
    """What the Payer is being asked to approve, and why."""
    link = _link(store, token)
    now = clock.now()
    with store.transaction(link.tenant_id) as tx:
        _still_open(tx, link, now)
        consent = tx.find_consent_request(link.consent_request_id)
        decision = tx.get_decision(consent.decision_id) if consent else None
        record = tx.get_audit_record(decision.audit_record_id) if decision else None
        asked = record.record.inputs if record else None
        if consent is None or decision is None or asked is None:
            raise RuntimeError(f"Consent request {link.consent_request_id} has no Decision")
        agent = tx.get_agent(decision.agent_id)
        mandate = tx.get_mandate(decision.mandate_id)
    ref = decision.policy_pack
    pack = load_pack(settings.policy_packs_dir, ref.id, ref.version).pack
    request = asked.request
    intent = asked.signals.intent
    return ConsentLinkView(
        consent_request_id=consent.consent_request_id,
        status=consent.status(now),
        expires_at=consent.expires_at,
        payment=PaymentView(
            amount=request.amount,
            amount_text=money_text(request.amount),
            merchant_name=request.merchant.name,
            mcc=request.merchant.mcc,
            agent_name=agent.name if agent else None,
        ),
        reasons=[
            ReasonView(code=code, text=reason_text(code, asked, pack))
            for code in decision.reason_codes
        ],
        intent_verdict=intent.verdict if intent else None,
        mandate_inactive=mandate_inactive(mandate, agent, now),
    )


@router.post("/approve", response_model=ConsentResolutionOut)
def approve_via_link(
    request: Request,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    workflow: WorkflowDep,
    token: LinkToken = None,
    key: IdempotencyHeader = None,
) -> Response:
    """The Payer said yes. Re-checked like a Tenant approval (ADR-0010)."""
    return _resolve(request, store, clock, workflow, token, key, "approve", pack)


@router.post("/decline", response_model=ConsentResolutionOut)
def decline_via_link(
    request: Request,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    workflow: WorkflowDep,
    token: LinkToken = None,
    key: IdempotencyHeader = None,
) -> Response:
    """The Payer said no."""
    return _resolve(request, store, clock, workflow, token, key, "decline", pack)


def _resolve(
    request: Request,
    store: StoreDep,
    clock: ClockDep,
    workflow: WorkflowDep,
    token: str | None,
    key: str | None,
    action: Literal["approve", "decline"],
    pack: PackDep,
) -> Response:
    link = _link(store, token)
    idempotency_key = require_idempotency_key(key)
    csr = link.consent_request_id

    def write(read_at: datetime) -> tuple[int, ConsentResolutionOut]:
        now = decision_time(read_at)
        _still_open(tx, link, now)
        if action == "approve":
            resolution = approve(tx, csr, "consent_link", None, now, pack)
        else:
            resolution = decline(tx, csr, "consent_link", None, now)
        return 200, resolution_out(resolution)

    with store.transaction(link.tenant_id) as tx:
        response = run_idempotent(tx, idempotency_key, request_hash(request, None), clock, write)
    signal_resolved(workflow, link.tenant_id, csr)
    return response
