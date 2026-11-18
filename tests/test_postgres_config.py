"""Postgres in Compose and CI (#196, ADR-0022): one image, its data on the mount it expects."""

from pathlib import Path

import yaml

COMPOSE = Path("infra/docker-compose.yml")
CI = Path(".github/workflows/ci.yml")


def compose_postgres() -> dict[str, object]:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service: dict[str, object] = compose["services"]["postgres"]
    return service


def test_ci_tests_on_the_postgres_compose_runs() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    images = [
        job["services"]["postgres"]["image"]
        for job in ci["jobs"].values()
        if "postgres" in job.get("services", {})
    ]

    assert images
    assert set(images) == {compose_postgres()["image"]}


def test_postgres_18_keeps_its_data_in_a_volume_of_its_own_at_var_lib_postgresql() -> None:
    # The 18 image keeps PGDATA in /var/lib/postgresql/18/docker and refuses to start on
    # a volume at /var/lib/postgresql/data. The 16 volume, pgdata, is left for rollback.
    service = compose_postgres()

    assert service["image"] == "postgres:18.7"
    assert service["volumes"] == ["pgdata18:/var/lib/postgresql"]
