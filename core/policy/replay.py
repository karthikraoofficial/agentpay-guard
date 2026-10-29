"""Replay: re-run a recorded Decision and compare bytes (CLAUDE.md non-negotiable 5).

The recorded output is taken from the stored bytes themselves, and the engine is run
on the recorded inputs, including the recorded Decision time, Spent this month and Signals, so
nothing about the present can change the answer.
"""

import json

from pydantic import BaseModel, ConfigDict

from core.domain.audit import AuditRecord
from core.domain.canonical_json import canonical_json
from core.policy.engine import decide
from core.policy.pack import PolicyPack


class PackMismatch(Exception):
    """The pack offered for replay is not the one the Decision was made with."""


class ReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    recorded: bytes
    replayed: bytes

    @property
    def identical(self) -> bool:
        return self.recorded == self.replayed


def replay(body: bytes, pack: PolicyPack, pack_hash: str) -> ReplayResult:
    """Re-run the Decision stored in `body` with `pack`, whose hash must be the recorded one."""
    record = AuditRecord.model_validate_json(body)
    recorded_pack = record.pack
    if (pack.id, pack.version, pack_hash) != (
        recorded_pack.id,
        recorded_pack.version,
        recorded_pack.hash,
    ):
        raise PackMismatch(
            f"recorded {recorded_pack.id}@{recorded_pack.version} {recorded_pack.hash}, "
            f"offered {pack.id}@{pack.version} {pack_hash}"
        )
    i = record.inputs
    output = decide(i.request, i.mandate, i.agent, i.spent_this_month, i.signals, pack, i.now)
    return ReplayResult(
        recorded=canonical_json(json.loads(body)["output"]),
        replayed=canonical_json(output),
    )
