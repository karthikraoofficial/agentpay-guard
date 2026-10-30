"""Replay and chain verification as pure functions, on records built in memory."""

import json
from typing import Any

import pytest

from core.domain.audit import (
    ZERO_HASH,
    AuditRecord,
    DecisionInputs,
    StoredAuditRow,
    chain_hash,
    first_broken_seq,
    seal,
)
from core.domain.consent import ResolutionEvidence
from core.domain.decisions import PackRef
from core.domain.signals import Signals
from core.policy.engine import decide
from core.policy.replay import PackMismatch, replay
from tests.policy.test_engine import NOW, PACK, ZERO, agent, mandate, request

PACK_HASH = "sha256:a9037729f2af9b6ad63761c6a396c9483a3277c7a876e9d119e8b424342551ea"


def _record(seq: int, prev_hash: str, **request_overrides: Any) -> AuditRecord:
    req = request(**request_overrides)
    inputs = DecisionInputs(
        request=req, mandate=mandate(), agent=agent(), spent_this_month=ZERO, now=NOW
    )
    return AuditRecord(
        audit_record_id=f"aud_{seq}",
        tenant_id="ten_1",
        seq=seq,
        prev_hash=prev_hash,
        kind="decision",
        decision_id=f"dec_{seq}",
        recorded_at=NOW,
        inputs=inputs,
        pack=PackRef(id="default", version="1.0.0", hash=PACK_HASH),
        output=decide(req, inputs.mandate, inputs.agent, ZERO, Signals(), PACK, NOW),
    )


def _chain(n: int) -> list[StoredAuditRow]:
    rows, prev = [], ZERO_HASH
    for seq in range(1, n + 1):
        sealed = seal(_record(seq, prev, amount={"minor": seq * 1_000_00, "currency": "INR"}))
        rows.append(StoredAuditRow.from_sealed(sealed))
        prev = sealed.record_hash
    return rows


def _head(rows: list[StoredAuditRow]) -> tuple[int, str]:
    return (rows[-1].seq, rows[-1].record_hash) if rows else (0, ZERO_HASH)


# --- Replay ------------------------------------------------------------------------


def test_a_recorded_decision_replays_byte_identical() -> None:
    sealed = seal(_record(1, ZERO_HASH))

    result = replay(sealed.body, PACK, PACK_HASH)

    assert result.identical
    assert result.recorded == result.replayed


def test_replay_reports_a_difference_when_the_recorded_output_was_wrong() -> None:
    sealed = seal(_record(1, ZERO_HASH))
    doctored = json.loads(sealed.body)
    doctored["output"]["outcome"] = "block"

    result = replay(json.dumps(doctored).encode(), PACK, PACK_HASH)

    assert not result.identical
    assert b'"outcome":"block"' in result.recorded
    assert b'"outcome":"allow"' in result.replayed


def test_replay_refuses_a_pack_whose_hash_differs_from_the_recorded_one() -> None:
    sealed = seal(_record(1, ZERO_HASH))

    with pytest.raises(PackMismatch, match="sha256:other"):
        replay(sealed.body, PACK, "sha256:other")


# --- Chain verification ----------------------------------------------------------------


def test_an_untouched_chain_verifies_clean() -> None:
    rows = _chain(4)

    assert first_broken_seq(rows, _head(rows)) is None
    assert first_broken_seq([], (0, ZERO_HASH)) is None


def test_a_changed_field_in_one_record_is_reported_at_that_record() -> None:
    rows = _chain(4)
    body = json.loads(rows[1].body)
    body["output"]["outcome"] = "block"
    rows[1] = rows[1].model_copy(update={"body": json.dumps(body).encode()})

    assert first_broken_seq(rows, _head(rows)) == 2


def test_a_deleted_record_is_reported_at_its_seq() -> None:
    rows = _chain(4)
    head = _head(rows)

    assert first_broken_seq(rows[:2] + rows[3:], head) == 3
    assert first_broken_seq(rows[:3], head) == 4


@pytest.mark.parametrize("column", ["kind", "decision_id", "audit_record_id"])
def test_a_changed_index_column_is_reported(column: str) -> None:
    rows = _chain(3)
    rows[2] = rows[2].model_copy(update={column: "void" if column == "kind" else "x_forged"})

    assert first_broken_seq(rows, _head(rows)) == 3


def test_a_record_rehashed_after_editing_breaks_the_next_link() -> None:
    rows = _chain(3)
    edited = seal(_record(2, rows[0].record_hash, quote_id="q_forged"))
    rows[1] = StoredAuditRow.from_sealed(edited)

    assert first_broken_seq(rows, _head(rows)) == 3


def test_an_edited_last_record_is_caught_by_the_chain_head() -> None:
    rows = _chain(2)
    head = _head(rows)
    rows[1] = StoredAuditRow.from_sealed(seal(_record(2, rows[0].record_hash, quote_id="q")))

    assert first_broken_seq(rows, head) == 2


def _as_written_in_phase_1(record: AuditRecord) -> bytes:
    """The bytes a Phase 1 Decision was stored as: no `signals` among its inputs."""
    from core.domain.canonical_json import canonical_json

    doc = json.loads(canonical_json(record))
    del doc["inputs"]["signals"]
    return canonical_json(doc)


def test_a_phase_1_record_without_signals_still_replays_and_verifies() -> None:
    body = _as_written_in_phase_1(_record(1, ZERO_HASH))
    sealed = seal(AuditRecord.model_validate_json(body))
    row = StoredAuditRow.from_sealed(sealed).model_copy(
        update={"body": body, "record_hash": chain_hash(ZERO_HASH, body)}
    )

    assert replay(body, PACK, PACK_HASH).identical
    assert first_broken_seq([row], (1, row.record_hash)) is None


# --- Consent resolution records -----------------------------------------------------


def _declined(seq: int, prev_hash: str) -> AuditRecord:
    return AuditRecord(
        audit_record_id=f"aud_{seq}",
        tenant_id="ten_1",
        seq=seq,
        prev_hash=prev_hash,
        kind="consent_resolution",
        decision_id="dec_1",
        recorded_at=NOW,
        resolution=ResolutionEvidence(
            consent_request_id="csr_1", status="declined", resolved_via="tenant_api"
        ),
    )


def test_a_decision_record_keeps_its_byte_format_with_no_resolution_key() -> None:
    body = json.loads(seal(_record(1, ZERO_HASH)).body)

    assert "resolution" not in body
    assert {"inputs", "pack", "output"} <= body.keys()


def test_a_declined_resolution_has_no_engine_fields_and_chains_with_decisions() -> None:
    first = seal(_record(1, ZERO_HASH))
    second = seal(_declined(2, first.record_hash))

    body = json.loads(second.body)
    assert body["resolution"] == {
        "consent_request_id": "csr_1",
        "status": "declined",
        "resolved_via": "tenant_api",
        "attestation": None,
    }
    assert not {"inputs", "pack", "output"} & body.keys()
    rows = [StoredAuditRow.from_sealed(s) for s in (first, second)]
    assert first_broken_seq(rows, (2, second.record_hash)) is None


def test_a_record_must_match_its_kind() -> None:
    decision = _record(1, ZERO_HASH)

    with pytest.raises(ValueError, match="decision record"):
        AuditRecord.model_validate(decision.model_dump() | {"inputs": None})
    with pytest.raises(ValueError, match="decision record"):
        AuditRecord.model_validate(
            decision.model_dump() | {"resolution": _declined(1, ZERO_HASH).resolution}
        )
    with pytest.raises(ValueError, match="consent_resolution record"):
        AuditRecord.model_validate(_declined(1, ZERO_HASH).model_dump() | {"resolution": None})
    with pytest.raises(ValueError, match="declined"):
        AuditRecord.model_validate(
            _declined(1, ZERO_HASH).model_dump()
            | {"inputs": decision.inputs, "pack": decision.pack, "output": decision.output}
        )
