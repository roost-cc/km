"""Integration tests for km.infisical.InfisicalClient — env-gated on
INFISICAL_HOST / INFISICAL_TEST_TOKEN / INFISICAL_TEST_PROJECT_ID."""

from __future__ import annotations

import os
import time

import pytest

from km.infisical import InfisicalClient

HOST = os.environ.get("INFISICAL_HOST", "")
PROJECT_ID = os.environ.get("INFISICAL_TEST_PROJECT_ID", "")
ENVIRONMENT = "dev"

pytestmark = pytest.mark.integration

if not HOST or not os.environ.get("INFISICAL_TEST_TOKEN") or not PROJECT_ID:
    pytest.skip(
        "INFISICAL_HOST / INFISICAL_TEST_TOKEN / INFISICAL_TEST_PROJECT_ID not set",
        allow_module_level=True,
    )


TEST_PATH = f"/test-s307-{int(time.time())}"


@pytest.fixture(scope="module")
def client() -> InfisicalClient:
    c = InfisicalClient(
        host=HOST,
        token=os.environ["INFISICAL_TEST_TOKEN"],
        project_id=PROJECT_ID,
        environment=ENVIRONMENT,
    )
    yield c
    c.close()


@pytest.fixture(scope="module")
def store(client: InfisicalClient) -> InfisicalClient:
    # SecretStore (the old wrapper) was removed; the integration test
    # talks to InfisicalClient directly, which exposes the same
    # list/get/set/delete surface.
    return client


@pytest.fixture(scope="module", autouse=True)
def _cleanup(client: InfisicalClient, store: InfisicalClient):
    yield
    # Best-effort cleanup: list everything at TEST_PATH and below, then delete.
    try:
        leftover = store.list(TEST_PATH, recursive=True)
    except Exception:
        leftover = []
    for s in leftover:
        try:
            client.delete(s.name, s.path)
        except Exception:
            pass


def test_set_and_get(client, store):
    name = "test_secret_1"
    store.set(name, "hello-world", TEST_PATH)
    s = store.get(name, TEST_PATH)
    assert s is not None
    assert s.name == name
    assert s.value == "hello-world"
    assert s.path == TEST_PATH


def test_set_update(client, store):
    name = "test_secret_1"
    store.set(name, "updated-value", TEST_PATH)
    s = store.get(name, TEST_PATH)
    assert s is not None
    assert s.value == "updated-value"


def test_set_with_comment(client, store):
    name = "test_with_comment"
    store.set(name, "val", TEST_PATH, comment="this is a test comment")
    s = store.get(name, TEST_PATH)
    assert s is not None
    assert s.metadata.comment == "this is a test comment"


def test_delete_then_get_returns_none(client, store):
    name = "test_to_delete"
    store.set(name, "temp", TEST_PATH)
    store.delete(name, TEST_PATH)
    assert store.get(name, TEST_PATH) is None


def test_delete_nonexistent_no_error(client, store):
    store.delete("nonexistent_secret_xyz", TEST_PATH)  # should not raise


def test_list_returns_all(client, store):
    names = ["list_a", "list_b", "list_c"]
    for n in names:
        store.set(n, f"val_{n}", TEST_PATH)
    results = store.list(TEST_PATH)
    result_names = {s.name for s in results}
    for n in names:
        assert n in result_names


def test_list_recursive(client, store):
    sub = f"{TEST_PATH}/sub"
    name = "nested_secret"
    store.set(name, "nested-val", sub)
    shallow = store.list(TEST_PATH)
    assert name not in {s.name for s in shallow}
    deep = store.list(TEST_PATH, recursive=True)
    assert name in {s.name for s in deep}
