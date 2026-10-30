"""Unit tests for km.producers.aws_iam (subprocess-mocked)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from km.producers.aws_iam import AwsIam
from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    ProducerContractError,
    Secret,
)


def _entry(name: str, fmt: str | None, manifest: Path, group: str = "g") -> ManifestEntry:
    return ManifestEntry(name=name, format=fmt, group_name=group, manifest_path=manifest)


def _ctx(
    *,
    iam_user: str | None = "roost-svc",
    revoke_old: bool = False,
    current_secrets: Mapping[str, str] | None = None,
) -> ProduceContext:
    group: dict[str, Any] = {}
    if iam_user is not None:
        group["iam_user"] = iam_user
    return ProduceContext(
        manifest={},
        current_secrets=current_secrets or {},
        group=group,
        revoke_old=revoke_old,
    )


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_aws(monkeypatch):
    """Capture every `aws ...` invocation and return scripted JSON."""

    class FakeAws:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.responses: dict[tuple[str, ...], str] = {}
            self.fail: dict[tuple[str, ...], tuple[int, str]] = {}

        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            if argv[0] != "aws":
                raise FileNotFoundError(argv[0])
            # strip --profile <p> if present; key on the remaining tokens
            rest = argv[1:]
            if rest[:1] == ["--profile"]:
                rest = rest[2:]
            key = tuple(rest)
            for k, (rc, err) in self.fail.items():
                if key[: len(k)] == k:
                    raise subprocess.CalledProcessError(
                        returncode=rc, cmd=argv, output="", stderr=err
                    )
            for k, body in self.responses.items():
                if key[: len(k)] == k:
                    return _Result(stdout=body)
            return _Result(stdout="{}")

    fake = FakeAws()
    monkeypatch.setattr(subprocess, "run", fake)
    # Default: sts get-caller-identity succeeds, list-access-keys empty
    fake.responses[("sts", "get-caller-identity")] = json.dumps(
        {"Arn": "arn:aws:iam::303106298086:user/roost-svc"}
    )
    fake.responses[("iam", "list-access-keys")] = json.dumps({"AccessKeyMetadata": []})
    return fake


def _ok_create(access_id: str = "AKIA_NEW", secret: str = "secret_NEW") -> str:
    return json.dumps(
        {
            "AccessKey": {
                "AccessKeyId": access_id,
                "SecretAccessKey": secret,
                "UserName": "roost-svc",
                "Status": "Active",
            }
        }
    )


_M = Path("/tmp/m.yaml")


def test_happy_path_returns_keyed_pair(fake_aws) -> None:
    fake_aws.responses[("iam", "create-access-key")] = _ok_create()
    out = AwsIam({}).produce(
        [
            _entry("access_key_id_dev", "aws-access-key-id", _M),
            _entry("secret_access_key_id_dev", "aws-secret-access-key", _M),
        ],
        _ctx(),
    )
    assert out == [
        Secret(name="access_key_id_dev", value="AKIA_NEW"),
        Secret(name="secret_access_key_id_dev", value="secret_NEW"),
    ]


def test_uses_aws_profile_when_configured(fake_aws) -> None:
    fake_aws.responses[("iam", "create-access-key")] = _ok_create()
    AwsIam({"aws_profile": "roost"}).produce(
        [
            _entry("a", "aws-access-key-id", _M),
            _entry("b", "aws-secret-access-key", _M),
        ],
        _ctx(),
    )
    for argv in fake_aws.calls:
        assert argv[:3] == ["aws", "--profile", "roost"]


def test_missing_iam_user_raises(fake_aws) -> None:
    with pytest.raises(ProducerContractError, match="`iam_user` is required"):
        AwsIam({}).produce(
            [
                _entry("a", "aws-access-key-id", _M),
                _entry("b", "aws-secret-access-key", _M),
            ],
            _ctx(iam_user=None),
        )


def test_unknown_format_raises(fake_aws) -> None:
    with pytest.raises(ProducerContractError, match="unsupported format"):
        AwsIam({}).produce([_entry("a", "banana", _M)], _ctx())


def test_missing_pair_raises(fake_aws) -> None:
    with pytest.raises(ProducerContractError, match="exactly one"):
        AwsIam({}).produce([_entry("a", "aws-access-key-id", _M)], _ctx())


def test_sts_failure_aborts_before_minting(fake_aws) -> None:
    fake_aws.fail[("sts", "get-caller-identity")] = (255, "Token has expired")
    with pytest.raises(ProducerContractError, match="AWS session not active"):
        AwsIam({}).produce(
            [
                _entry("a", "aws-access-key-id", _M),
                _entry("b", "aws-secret-access-key", _M),
            ],
            _ctx(),
        )
    assert all("create-access-key" not in " ".join(c) for c in fake_aws.calls)


def test_list_access_keys_no_such_entity_aborts_before_minting(fake_aws) -> None:
    fake_aws.fail[("iam", "list-access-keys")] = (
        255,
        "An error occurred (NoSuchEntity) when calling the ListAccessKeys "
        "operation: The user with name roost-svc cannot be found.",
    )
    with pytest.raises(ProducerContractError, match="NoSuchEntity"):
        AwsIam({}).produce(
            [
                _entry("a", "aws-access-key-id", _M),
                _entry("b", "aws-secret-access-key", _M),
            ],
            _ctx(),
        )
    assert all("create-access-key" not in " ".join(c) for c in fake_aws.calls)
    assert all("delete-access-key" not in " ".join(c) for c in fake_aws.calls)


def test_revoke_old_deletes_pre_existing_keys(fake_aws) -> None:
    fake_aws.responses[("iam", "list-access-keys")] = json.dumps(
        {
            "AccessKeyMetadata": [
                {"AccessKeyId": "AKIA_OLD_1"},
                {"AccessKeyId": "AKIA_OLD_2"},
            ]
        }
    )
    fake_aws.responses[("iam", "create-access-key")] = _ok_create()
    AwsIam({}).produce(
        [
            _entry("a", "aws-access-key-id", _M),
            _entry("b", "aws-secret-access-key", _M),
        ],
        _ctx(revoke_old=True),
    )
    deletes = [c for c in fake_aws.calls if "delete-access-key" in c]
    deleted_ids = sorted(c[c.index("--access-key-id") + 1] for c in deletes)
    assert deleted_ids == ["AKIA_OLD_1", "AKIA_OLD_2"]


def test_revoke_old_default_false_keeps_old_keys(fake_aws) -> None:
    fake_aws.responses[("iam", "list-access-keys")] = json.dumps(
        {"AccessKeyMetadata": [{"AccessKeyId": "AKIA_OLD"}]}
    )
    fake_aws.responses[("iam", "create-access-key")] = _ok_create()
    AwsIam({}).produce(
        [
            _entry("a", "aws-access-key-id", _M),
            _entry("b", "aws-secret-access-key", _M),
        ],
        _ctx(),
    )
    assert all("delete-access-key" not in " ".join(c) for c in fake_aws.calls)


def test_create_access_key_malformed_payload_raises(fake_aws) -> None:
    fake_aws.responses[("iam", "create-access-key")] = json.dumps(
        {
            "AccessKey": {
                # AccessKeyId omitted (would be a string in practice)
                "SecretAccessKey": "secret_NEW",
                "UserName": "roost-svc",
            }
        }
    )
    with pytest.raises(ProducerContractError, match="malformed payload"):
        AwsIam({}).produce(
            [
                _entry("a", "aws-access-key-id", _M),
                _entry("b", "aws-secret-access-key", _M),
            ],
            _ctx(),
        )


def test_aws_cli_missing_raises(monkeypatch) -> None:
    def _missing(argv, **kw):
        raise FileNotFoundError("aws")

    monkeypatch.setattr(subprocess, "run", _missing)
    with pytest.raises(ProducerContractError, match="`aws` CLI not on PATH"):
        AwsIam({}).produce(
            [
                _entry("a", "aws-access-key-id", _M),
                _entry("b", "aws-secret-access-key", _M),
            ],
            _ctx(),
        )


def test_invalid_aws_profile_rejected_at_construction() -> None:
    with pytest.raises(ProducerContractError, match="aws_profile"):
        AwsIam({"aws_profile": 42})
