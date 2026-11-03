"""The citation checker: whether a model's narrative may go into an Evidence pack.

A narrative is accepted only if the reply finished cleanly, is the JSON object the
prompt asks for, has at least one claim, and every claim has a known kind and cites at
least one Audit record, each of them in the bundle and of a kind that claim may cite
(ADR-0018). One failing claim fails the whole narrative: a claim is never dropped.
"""

import json
from typing import Any, cast, get_args

from pydantic import ValidationError

from core.domain.decisions import AuditRecordId
from core.domain.evidence import (
    CLAIM_CITES,
    CheckFailure,
    Claim,
    ClaimKind,
    EvidenceBundle,
)

_KINDS = frozenset(get_args(ClaimKind))


def check_narrative(
    text: str, stop_reason: str, bundle: EvidenceBundle
) -> tuple[tuple[Claim, ...], tuple[CheckFailure, ...]]:
    """The claims and no failures, or no claims and why the narrative was refused."""
    if stop_reason != "end_turn":
        return (), (CheckFailure(code="stopped", detail=stop_reason),)
    items = _claims(text)
    if items is None:
        return (), (CheckFailure(code="unparseable"),)
    if not items:
        return (), (CheckFailure(code="empty"),)
    kinds = {
        r.audit_record_id: r.kind for r in (*bundle.trail, *bundle.related, *bundle.payer_payments)
    }
    failures: list[CheckFailure] = []
    claims: list[Claim] = []
    for index, item in enumerate(items):
        raw_kind = item.get("kind")
        cites = item.get("cites")
        if raw_kind not in _KINDS:
            failures.append(CheckFailure(code="unknown_kind", claim=index, detail=str(raw_kind)))
            continue
        kind = cast(ClaimKind, raw_kind)  # checked against the kinds just above
        if not isinstance(cites, list) or not cites:
            failures.append(CheckFailure(code="uncited", claim=index))
            continue
        allowed = CLAIM_CITES[kind]
        for cite in cites:
            found = kinds.get(AuditRecordId(str(cite)))
            if found is None:
                failures.append(CheckFailure(code="not_in_bundle", claim=index, detail=str(cite)))
            elif found not in allowed:
                failures.append(
                    CheckFailure(code="wrong_kind", claim=index, detail=f"{cite} is {found}")
                )
        try:
            claims.append(Claim(text=item.get("text", ""), cites=tuple(cites), kind=kind))
        except ValidationError:
            failures.append(CheckFailure(code="uncited", claim=index, detail="no text"))
    if failures:
        return (), tuple(failures)
    return tuple(claims), ()


def _claims(text: str) -> list[dict[str, Any]] | None:
    """The reply's claims, or None if it isn't `{"claims": [...]}` (a ```json fence
    around it is tolerated)."""
    body = text.strip()
    if body.startswith("```"):
        body = body.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    claims = parsed.get("claims") if isinstance(parsed, dict) else None
    if not isinstance(claims, list) or not all(isinstance(c, dict) for c in claims):
        return None
    return claims
