"""Integration test: rotate → publish round-trip against the dev Infisical.

S-432: rebuilds the historical bootstrap chain against the post-S-362
unified rotation model. The chain under test is:

    1. `km rotate` (batch) — every minted producer (uuid, base64_32,
       passphrase, ed25519) writes fresh values into each manifest's
       `secrets:` block.
    2. `km publish t dev --all --execute` — every value is published to
       its declared Infisical address through the target's one
       destination (ensure creates the folders first).
    3. Verify — list the workspace and assert every declared address
       resolves to a non-empty value.

The target host is the Infisical instance ``INFISICAL_HOST`` names (any
instance; see ``docs/integration-tests.md``). Without ``INFISICAL_HOST``
the test is skipped: that is the opt-in.

The Infisical project id and machine-identity token vary per
developer, so they're sourced from environment variables rather
than hard-coded, and written into the test project's
``targets/t.yaml`` (where km reads them, F-54 D4):

- ``INFISICAL_TEST_PROJECT_ID`` — workspace id of a throwaway test
  project the developer is willing to write to and clean up.
- ``INFISICAL_TEST_TOKEN`` — bearer token for a machine identity
  with write access to that project.

(Same env vars as ``infisical_int_test.py`` and
``publishers_infisical_int_test.py`` — one credential set drives every
Infisical-touching integration test.)

Once ``INFISICAL_HOST`` is set there is **no further env gate**. If
either variable is unset, or the instance is down, the test errors
loudly — no silent skips for integration coverage you opted into.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from km.commands import _publish, _rotate
from km.infisical import InfisicalClient

pytestmark = pytest.mark.integration

INFISICAL_HOST = os.environ.get("INFISICAL_HOST", "")
INFISICAL_ENVIRONMENT = "dev"

if not INFISICAL_HOST:
    pytest.skip("INFISICAL_HOST not set", allow_module_level=True)


CONFIG_TEMPLATE = """environments: [dev]
targets:
  t:
    manifests: [uuid, base64, passphrase, ed25519]
    destinations:
      - publisher: infisical
        host: {host}
        envs: [dev]
"""


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is unset; populate it from a throwaway-project machine "
            "identity (INFISICAL_HOST is set, so the bootstrap integration "
            "test requires it)"
        )
    return value


# Each entry is (manifest filename, producer, secrets list, format-or-None).
# Producers chosen so the test exercises every minted-producer code
# path the bootstrap chain ever covered: uuid, base64_32, passphrase,
# ed25519. Synapse is intentionally out of scope (its state lives in
# external yaml the test environment doesn't have).
_MANIFESTS: list[tuple[str, str, list[str], str | None]] = [
    ("uuid.yaml", "uuid", ["uuid_token"], None),
    ("base64.yaml", "base64_32", ["b64_token"], None),
    ("passphrase.yaml", "passphrase", ["pp_token"], None),
    ("ed25519.yaml", "ed25519", ["ed25519_private_key"], "ed25519"),
]


def _manifest_body(group: str, producer: str, names: list[str], fmt: str | None) -> str:
    secrets_block = "\n".join(f"  {n}: null" for n in names)
    publishers_block = "\n".join(f"      {n}: /s432-bootstrap/{n}" for n in names)
    secret_entries = "\n".join(
        f"        - name: {n}" + (f"\n          format: {fmt}" if fmt else "") for n in names
    )
    return f"""service: s432-bootstrap-{group}
secrets:
{secrets_block}
publishers:
  infisical:
    dev:
{publishers_block}
rotation:
  runbook: r.md
  groups:
    {group}:
      max_age_days: 30
      last_rotated: null
      producer: {producer}
      secrets:
{secret_entries}
"""


def _all_addresses() -> list[str]:
    out: list[str] = []
    for _fname, _producer, names, _fmt in _MANIFESTS:
        out.extend(f"/s432-bootstrap/{n}" for n in names)
    return out


@pytest.fixture(scope="module")
def client() -> InfisicalClient:
    """Talk to the Infisical at ``INFISICAL_HOST`` directly for pre/post checks.

    Errors loudly if the env vars are missing or the instance is down
    — ``INFISICAL_HOST`` opts in; after that nothing is skip-on-miss.
    """
    c = InfisicalClient(
        host=INFISICAL_HOST,
        token=_require_env("INFISICAL_TEST_TOKEN"),
        project_id=_require_env("INFISICAL_TEST_PROJECT_ID"),
        environment=INFISICAL_ENVIRONMENT,
    )
    yield c
    c.close()


def _list_or_empty(client: InfisicalClient, path: str) -> list:
    """Listing a non-existent folder returns 404 — treat that as empty
    rather than an error, since a fresh test run starts from no folder."""
    try:
        return client.list(path, recursive=True)
    except RuntimeError as e:
        if "SecretPathNotFound" in str(e) or "404" in str(e):
            return []
        raise


@pytest.fixture(autouse=True)
def _cleanup(client: InfisicalClient):
    """Best-effort delete every secret the test wrote, after the test runs."""
    yield
    leftover = _list_or_empty(client, "/s432-bootstrap")
    for s in leftover:
        try:
            client.delete(s.name, s.path)
        except Exception:
            pass


def test_rotate_then_push_round_trip(tmp_path: Path, monkeypatch, client) -> None:
    # --- pre-flight: refuse to run against a non-empty target folder ---
    existing = _list_or_empty(client, "/s432-bootstrap")
    if existing:
        pytest.fail(
            f"Refusing to run bootstrap test: /s432-bootstrap is not empty "
            f"({len(existing)} secret(s) present) in project "
            f"{client.project_id}. Clean the folder before re-running."
        )

    # --- project layout: config.yaml + manifests/ in tmp_path ---
    proj = tmp_path / "proj"
    (proj / "manifests").mkdir(parents=True)
    (proj / "runbooks").mkdir()
    (proj / "runbooks" / "r.md").write_text("# stub\n")
    (proj / "config.yaml").write_text(CONFIG_TEMPLATE.format(host=INFISICAL_HOST))
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text(
        "_publishers:\n"
        "  infisical:\n"
        f"    project_id: {_require_env('INFISICAL_TEST_PROJECT_ID')}\n"
        f"    token: {_require_env('INFISICAL_TEST_TOKEN')}\n"
    )

    for fname, producer, names, fmt in _MANIFESTS:
        (proj / "manifests" / fname).write_text(
            _manifest_body(group=Path(fname).stem, producer=producer, names=names, fmt=fmt)
        )

    monkeypatch.chdir(proj)
    cfg_path = str(proj / "config.yaml")

    # --- step 1: rotate ---
    rc = _rotate.run(
        argparse.Namespace(
            config=cfg_path,
            slug=None,
            project=None,
            env=INFISICAL_ENVIRONMENT,
            force=None,
            dry_run=False,
            revoke_old=False,
        )
    )
    assert rc == 0, "rotation failed"

    # --- step 2: publish ---
    rc = _publish.run(
        argparse.Namespace(
            config=cfg_path,
            target="t",
            envs=[INFISICAL_ENVIRONMENT],
            all=True,
            execute=True,
        )
    )
    assert rc == 0, "publish failed"

    # --- step 3: verify ---
    runtime = {
        f"{s.path.rstrip('/')}/{s.name}" if s.path != "/" else f"/{s.name}": s.value
        for s in client.list("/s432-bootstrap", recursive=True)
    }
    for address in _all_addresses():
        assert address in runtime, f"missing {address} in store after publish"
        assert runtime[address], f"empty value at {address}"
