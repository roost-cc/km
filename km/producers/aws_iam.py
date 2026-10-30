"""AWS IAM producer — mint a fresh access-key/secret pair for an IAM user.

Drives ``aws iam`` over subprocess. The IAM user comes from the rotation
group's ``iam_user`` key (``ctx.group``); the producer never reads from
env vars or argv. Each entry in the rotation group must declare a ``format:`` of
either ``aws-access-key-id`` or ``aws-secret-access-key`` so the
producer knows which AWS field to write into which manifest secret.

``ctx.revoke_old`` (set by ``--revoke-old``) deletes every other access
key for the same IAM user after the new pair is minted —
breach-response posture. The default leaves the prior key valid
(routine rotation overlap window).

Config block (``cfg.producers.aws-iam``):

- ``aws_profile``: passed as ``--profile`` to every ``aws`` call.

Orphan-on-revoke recovery
=========================

If revoke fails after the new key pair has been minted (AWS IAM 5xx,
eventual-consistency wobble, partial network), the producer raises
``ProducerContractError`` and the dispatcher does *not* update the
manifest. The new pair is live at AWS but absent from the manifest — an
orphan. The producer logs the new ``AccessKeyId`` to stderr immediately
after ``create-access-key`` returns so the orphan is recoverable from
the operator's terminal scrollback.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    Producer,
    ProducerContractError,
    Secret,
)

ACCESS_KEY_FORMAT = "aws-access-key-id"
SECRET_KEY_FORMAT = "aws-secret-access-key"


def _aws(args: list[str], *, profile: str | None) -> dict:
    cmd = ["aws"]
    if profile:
        cmd += ["--profile", profile]
    cmd += [*args, "--output", "json"]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as e:
        raise ProducerContractError("aws-iam producer: `aws` CLI not on PATH") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise ProducerContractError(
            f"aws-iam producer: `aws {' '.join(args)}` failed: {stderr or e}"
        ) from e
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise ProducerContractError(
            f"aws-iam producer: `aws {' '.join(args)}` returned non-JSON: {out!r}"
        ) from e


def _require_session(profile: str | None) -> None:
    cmd = ["aws"]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["sts", "get-caller-identity", "--output", "json"]
    try:
        subprocess.run(  # noqa: S603
            cmd, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as e:
        raise ProducerContractError("aws-iam producer: `aws` CLI not on PATH") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise ProducerContractError(
            "aws-iam producer: AWS session not active "
            "(run `aws sso login`); "
            f"sts get-caller-identity exited {e.returncode}: {stderr}"
        ) from e


def _classify_entries(
    entries: list[ManifestEntry],
) -> tuple[ManifestEntry, ManifestEntry]:
    access_entry: ManifestEntry | None = None
    secret_entry: ManifestEntry | None = None
    for e in entries:
        if e.format == ACCESS_KEY_FORMAT:
            if access_entry is not None:
                raise ProducerContractError(
                    f"aws-iam producer: group {e.group_name!r} declares "
                    f"more than one secret with format {ACCESS_KEY_FORMAT!r}"
                )
            access_entry = e
        elif e.format == SECRET_KEY_FORMAT:
            if secret_entry is not None:
                raise ProducerContractError(
                    f"aws-iam producer: group {e.group_name!r} declares "
                    f"more than one secret with format {SECRET_KEY_FORMAT!r}"
                )
            secret_entry = e
        else:
            raise ProducerContractError(
                f"aws-iam producer: secret {e.name!r} has unsupported format "
                f"{e.format!r} — expected {ACCESS_KEY_FORMAT!r} or "
                f"{SECRET_KEY_FORMAT!r}"
            )
    if access_entry is None or secret_entry is None:
        raise ProducerContractError(
            "aws-iam producer: rotation group must declare exactly one "
            f"{ACCESS_KEY_FORMAT!r} secret and one {SECRET_KEY_FORMAT!r} secret"
        )
    return access_entry, secret_entry


def _read_iam_user(ctx: ProduceContext, manifest_path) -> str:
    group = ctx.group if isinstance(ctx.group, dict) else {}
    iam_user = group.get("iam_user")
    if not isinstance(iam_user, str) or not iam_user:
        raise ProducerContractError(
            f"aws-iam producer: {manifest_path}: "
            "the rotation group's `iam_user` is required (the IAM user whose "
            "access keys this producer rotates)"
        )
    return iam_user


class AwsIam(Producer):
    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        profile = config.get("aws_profile")
        if profile is not None and not isinstance(profile, str):
            raise ProducerContractError("aws-iam producer: config.aws_profile must be a string")
        self._profile: str | None = profile or None

    def produce(self, entries, ctx):
        if not entries:
            return []

        access_entry, secret_entry = _classify_entries(entries)
        iam_user = _read_iam_user(ctx, entries[0].manifest_path)

        _require_session(self._profile)
        existing = _aws(
            ["iam", "list-access-keys", "--user-name", iam_user],
            profile=self._profile,
        )
        pre_keys = {
            k["AccessKeyId"]
            for k in (existing.get("AccessKeyMetadata") or [])
            if isinstance(k, dict) and isinstance(k.get("AccessKeyId"), str)
        }
        sys.stderr.write(f"aws-iam: user={iam_user!r} existing_keys={len(pre_keys)}\n")

        created = _aws(
            ["iam", "create-access-key", "--user-name", iam_user],
            profile=self._profile,
        )
        key = created.get("AccessKey") or {}
        new_id = key.get("AccessKeyId")
        new_secret = key.get("SecretAccessKey")
        if not isinstance(new_id, str) or not isinstance(new_secret, str):
            raise ProducerContractError(
                f"aws-iam producer: create-access-key returned malformed payload: {created!r}"
            )
        sys.stderr.write(f"aws-iam: minted new access key id={new_id}\n")

        if ctx.revoke_old:
            for old_id in sorted(pre_keys):
                _aws(
                    [
                        "iam",
                        "delete-access-key",
                        "--user-name",
                        iam_user,
                        "--access-key-id",
                        old_id,
                    ],
                    profile=self._profile,
                )
                sys.stderr.write(f"aws-iam: deleted old access key id={old_id}\n")

        return [
            Secret(name=access_entry.name, value=new_id),
            Secret(name=secret_entry.name, value=new_secret),
        ]
