import shutil
from pathlib import Path

import pytest

from api.main import create_app
from core.policy.packs import PackError
from tests.conftest import make_settings

PACKS = Path(__file__).resolve().parents[2] / "policy_packs"
ACTIVE_FILE = "1.2.0.yaml"  # the pack new Decisions are made with


def test_the_app_starts_with_the_active_pack_loaded() -> None:
    app = create_app(make_settings(policy_packs_dir=PACKS))

    assert app.state.policy_pack.pack.version == "1.2.0"


def test_the_app_refuses_to_start_when_the_active_pack_was_edited(tmp_path: Path) -> None:
    shutil.copytree(PACKS, tmp_path, dirs_exist_ok=True)
    pack_file = tmp_path / "default" / ACTIVE_FILE
    pack_file.write_text(
        pack_file.read_text(encoding="utf-8").replace("10000", "20000"), encoding="utf-8"
    )

    with pytest.raises(PackError, match="does not match the Manifest"):
        create_app(make_settings(policy_packs_dir=tmp_path))
