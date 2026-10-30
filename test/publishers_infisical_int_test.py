"""Integration tests for InfisicalPublisher.

Env-gated on ``INFISICAL_HOST``, ``INFISICAL_TEST_TOKEN`` and
``INFISICAL_TEST_PROJECT_ID``.
Exercises ``publish`` + ``collect`` end-to-end against live Infisical
(HTTP layer is covered separately in ``infisical_int_test.py``).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from km.infisical import InfisicalClient
from km.publishers import ManifestRef
from km.publishers.infisical import InfisicalPublisher

HOST = os.environ.get("INFISICAL_HOST", "")
PROJECT_ID = os.environ.get("INFISICAL_TEST_PROJECT_ID", "")
ENVIRONMENT = "dev"

pytestmark = pytest.mark.integration

if not HOST or not os.environ.get("INFISICAL_TEST_TOKEN") or not PROJECT_ID:
    pytest.skip(
        "INFISICAL_HOST / INFISICAL_TEST_TOKEN / INFISICAL_TEST_PROJECT_ID not set",
        allow_module_level=True,
    )


TEST_PATH = f"/test-s372-{int(time.time())}"


def _config() -> dict:
    return {"host": HOST}


@pytest.fixture
def publisher():
    pub = InfisicalPublisher(_config())
    # What the loader sets from targets/<target>.yaml `_publishers.infisical`.
    pub.target_state = {"project_id": PROJECT_ID, "token": os.environ["INFISICAL_TEST_TOKEN"]}
    yield pub
    # Best-effort cleanup: drop everything we wrote at TEST_PATH.
    try:
        cleanup_client = InfisicalClient(
            host=HOST,
            token=os.environ["INFISICAL_TEST_TOKEN"],
            project_id=PROJECT_ID,
            environment=ENVIRONMENT,
        )
        try:
            for info in cleanup_client.list(TEST_PATH, recursive=True):
                cleanup_client.delete(info.name, info.path)
        except RuntimeError as e:
            # Folder absence (404) is fine — nothing to clean.
            if "SecretPathNotFound" not in str(e) and "404" not in str(e):
                raise
        finally:
            cleanup_client.close()
    except Exception:
        pass
    pub.close()


def test_publish_then_collect_round_trip(publisher: InfisicalPublisher):
    section = {
        ENVIRONMENT: {
            "key_a": f"{TEST_PATH}/key_a",
            "key_b": f"{TEST_PATH}/key_b",
        }
    }
    secrets = {"key_a": "value-a", "key_b": "value-b"}
    manifest_path = Path("test-manifest.yaml")

    push_result = publisher.publish(
        manifest_path=manifest_path,
        env=ENVIRONMENT,
        secrets=secrets,
        section=section,
        dry_run=False,
    )
    assert push_result.written == 2
    assert push_result.failed == 0

    collect_result = publisher.collect(
        env=ENVIRONMENT,
        manifests=[ManifestRef(path=manifest_path, secrets=secrets, section=section)],
    )
    states = {(e.local_name, e.state) for e in collect_result.entries if e.local_name}
    assert ("key_a", "same") in states
    assert ("key_b", "same") in states


def test_collect_detects_drift(publisher: InfisicalPublisher):
    section = {ENVIRONMENT: {"key_c": f"{TEST_PATH}/key_c"}}
    push_secrets = {"key_c": "store-value"}
    publisher.publish(
        manifest_path=Path("m.yaml"),
        env=ENVIRONMENT,
        secrets=push_secrets,
        section=section,
        dry_run=False,
    )

    yaml_secrets = {"key_c": "yaml-value"}  # diverges
    collect_result = publisher.collect(
        env=ENVIRONMENT,
        manifests=[ManifestRef(path=Path("m.yaml"), secrets=yaml_secrets, section=section)],
    )
    diff = next(e for e in collect_result.entries if e.local_name == "key_c")
    assert diff.state == "differs"
    assert diff.value == "store-value"
    assert diff.manifest_value == "yaml-value"
