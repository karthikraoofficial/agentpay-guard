"""/v1/payers and /v1/agents: the people and software on each side of a Mandate."""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from api.deps import ClockDep, IdemKey, StoreDep, Tenant
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.parties import (
    Agent,
    AgentId,
    AgentStatus,
    ContactRef,
    Name,
    Payer,
    PayerId,
    new_id,
)

router = APIRouter(prefix="/v1", tags=["parties"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayerCreate(_Body):
    display_name: Name
    contact_ref: ContactRef


class PayerOut(BaseModel):
    payer_id: PayerId
    display_name: str
    contact_ref: str
    created_at: datetime


class AgentCreate(_Body):
    name: Name


class AgentOut(BaseModel):
    agent_id: AgentId
    name: str
    status: AgentStatus
    created_at: datetime


def _payer_out(payer: Payer) -> PayerOut:
    return PayerOut.model_validate(payer.model_dump(exclude={"tenant_id"}))


def _agent_out(agent: Agent) -> AgentOut:
    return AgentOut.model_validate(agent.model_dump(exclude={"tenant_id"}))


@router.post("/payers", status_code=201, response_model=PayerOut)
def create_payer(
    request: Request,
    body: PayerCreate,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    def write(now: datetime) -> tuple[int, PayerOut]:
        payer = Payer(
            payer_id=PayerId(new_id("pay")),
            tenant_id=tenant,
            display_name=body.display_name,
            contact_ref=body.contact_ref,
            created_at=now,
        )
        tx.add_payer(payer)
        return 201, _payer_out(payer)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/payers/{payer_id}")
def get_payer(payer_id: str, tenant: Tenant, store: StoreDep) -> PayerOut:
    with store.transaction(tenant) as tx:
        payer = tx.get_payer(PayerId(payer_id))
    if payer is None:
        raise ApiError(404, "PAYER_NOT_FOUND", f"No Payer {payer_id}.")
    return _payer_out(payer)


@router.post("/agents", status_code=201, response_model=AgentOut)
def create_agent(
    request: Request,
    body: AgentCreate,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    def write(now: datetime) -> tuple[int, AgentOut]:
        agent = Agent(
            agent_id=AgentId(new_id("agt")),
            tenant_id=tenant,
            name=body.name,
            status="active",
            created_at=now,
        )
        tx.add_agent(agent)
        return 201, _agent_out(agent)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, tenant: Tenant, store: StoreDep) -> AgentOut:
    with store.transaction(tenant) as tx:
        agent = tx.get_agent(AgentId(agent_id))
    if agent is None:
        raise ApiError(404, "AGENT_NOT_FOUND", f"No Agent {agent_id}.")
    return _agent_out(agent)


def _set_status(
    request: Request,
    agent_id: str,
    status: AgentStatus,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    def write(_now: datetime) -> tuple[int, AgentOut]:
        agent = tx.set_agent_status(AgentId(agent_id), status)
        if agent is None:
            raise ApiError(404, "AGENT_NOT_FOUND", f"No Agent {agent_id}.")
        return 200, _agent_out(agent)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


@router.post("/agents/{agent_id}/disable", response_model=AgentOut)
def disable_agent(
    request: Request, agent_id: str, tenant: Tenant, store: StoreDep, clock: ClockDep, key: IdemKey
) -> Response:
    """Stop this Agent under every Mandate until it is re-enabled."""
    return _set_status(request, agent_id, "disabled", tenant, store, clock, key)


@router.post("/agents/{agent_id}/enable", response_model=AgentOut)
def enable_agent(
    request: Request, agent_id: str, tenant: Tenant, store: StoreDep, clock: ClockDep, key: IdemKey
) -> Response:
    return _set_status(request, agent_id, "active", tenant, store, clock, key)
