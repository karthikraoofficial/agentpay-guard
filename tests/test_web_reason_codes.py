"""The /demo page explains every Reason code in plain words (#274): the web app's map
(web/src/lib/reason-codes.ts) holds exactly the codes the engine can give."""

import re
from pathlib import Path
from typing import get_args

from core.domain.decisions import ReasonCode

MAP = Path("web/src/lib/reason-codes.ts")


def test_the_web_app_explains_exactly_the_reason_codes_the_engine_gives() -> None:
    text = MAP.read_text(encoding="utf-8")
    block = text.split("REASON_CODE_TEXT", 1)[1].split("};", 1)[0]

    explained = set(re.findall(r"^\s+([A-Z_]+):", block, flags=re.MULTILINE))

    assert explained == set(get_args(ReasonCode))
