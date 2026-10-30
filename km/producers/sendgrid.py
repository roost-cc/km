"""SendGrid producer — mint a fresh API key with the prior key's scopes.

Config block (``cfg.producers.sendgrid``):

- ``key_secret``: secret name in the manifest's ``secrets:`` block that
  holds the current API key (default ``"email_password"``).
- ``key_name``: prefix used to identify prior keys in SendGrid's
  ``GET /v3/api_keys`` response (default ``"roost-rotated"``).
- ``timeout_s``: HTTP timeout in seconds (default ``15.0``).

When ``ctx.revoke_old`` is set, the prior key is deleted after the new
one is minted.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Mapping
from typing import Any

import httpx

from km.producers.contract import Producer, ProducerContractError, Secret

_API_ROOT = "https://api.sendgrid.com/v3"

_NAME_SUFFIX_FMT = "%Y-%m-%dT%H-%M-%SZ"


def _http_client(timeout: float, current_key: str) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers={"Authorization": f"Bearer {current_key}"},
    )


def _pick_prior_key_id(keys: list, key_name: str) -> str | None:
    dated: list[tuple[str, str]] = []
    exact: str | None = None
    for k in keys:
        if not isinstance(k, dict):
            continue
        name = k.get("name")
        kid = k.get("api_key_id")
        if not isinstance(name, str) or not isinstance(kid, str):
            continue
        if name == key_name:
            exact = kid
        elif name.startswith(f"{key_name}-"):
            dated.append((name, kid))
    if dated:
        dated.sort(key=lambda x: x[0], reverse=True)
        return dated[0][1]
    return exact


def _scopes_for(client: httpx.Client, key_id: str) -> list[str] | None:
    r = client.get(f"{_API_ROOT}/api_keys/{key_id}")
    sys.stderr.write(f"sendgrid: GET /v3/api_keys/{key_id} -> {r.status_code}\n")
    if r.status_code != 200:
        return None
    scopes = (r.json() or {}).get("scopes")
    if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes):
        return scopes
    return None


def _truncate(s: str, n: int = 200) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "..."


class Sendgrid(Producer):
    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self._key_secret = config.get("key_secret", "email_password")
        self._key_name = config.get("key_name", "roost-rotated")
        try:
            self._timeout = float(config.get("timeout_s", 15.0))
        except (TypeError, ValueError) as e:
            raise ProducerContractError(
                "sendgrid producer: config.timeout_s must be a number"
            ) from e

    def produce(self, entries, ctx):
        if not entries:
            return []
        if len(entries) != 1:
            raise ProducerContractError(
                "sendgrid producer: rotation group must declare exactly one "
                "secret (the API key); the SMTP username `apikey` is a "
                "SendGrid literal — split it into a `producer: static` group. "
                f"Got {len(entries)} entries."
            )

        current_key = ctx.current_secrets.get(self._key_secret)
        if not isinstance(current_key, str) or not current_key:
            raise ProducerContractError(
                f"sendgrid producer: current API key not found in current "
                f"secrets (looked at {self._key_secret!r}); set "
                "`config.key_secret` if the manifest uses a different name"
            )
        key_entry = entries[0]

        new_key_name = f"{self._key_name}-{dt.datetime.now(dt.UTC).strftime(_NAME_SUFFIX_FMT)}"

        with _http_client(self._timeout, current_key) as client:
            list_resp = client.get(f"{_API_ROOT}/api_keys")
            sys.stderr.write(f"sendgrid: GET /v3/api_keys -> {list_resp.status_code}\n")
            if list_resp.status_code != 200:
                raise ProducerContractError(
                    f"sendgrid producer: list api_keys failed "
                    f"({list_resp.status_code}): {_truncate(list_resp.text)}"
                )
            keys = (list_resp.json() or {}).get("result") or []
            prior_id = _pick_prior_key_id(keys, self._key_name)

            scopes = _scopes_for(client, prior_id) if prior_id else None
            if scopes is None:
                raise ProducerContractError(
                    "sendgrid producer: cannot infer scopes — no prior key "
                    f"named {self._key_name!r} found. Mint the first key by hand "
                    "(Settings → API Keys); the producer takes over from there."
                )
            if not scopes:
                raise ProducerContractError(
                    "sendgrid producer: prior key has no scopes — SendGrid "
                    "Full Access keys are not supported by this producer "
                    "(POST /v3/api_keys requires an explicit scopes list). "
                    "Replace the Full Access key with a Restricted Access "
                    "key whose scopes cover what you need; the producer "
                    "takes over from there."
                )

            create_resp = client.post(
                f"{_API_ROOT}/api_keys",
                json={"name": new_key_name, "scopes": scopes},
            )
            sys.stderr.write(f"sendgrid: POST /v3/api_keys -> {create_resp.status_code}\n")
            if create_resp.status_code not in (200, 201):
                raise ProducerContractError(
                    f"sendgrid producer: create api_key failed "
                    f"({create_resp.status_code}): {_truncate(create_resp.text)}"
                )
            body = create_resp.json() or {}
            new_key_value = body.get("api_key")
            new_key_id = body.get("api_key_id")
            if not isinstance(new_key_value, str) or not new_key_value:
                raise ProducerContractError(
                    "sendgrid producer: create response missing `api_key` "
                    "(the full key is only returned once at creation)"
                )
            sys.stderr.write(f"sendgrid: minted new key_id={new_key_id}\n")

            if ctx.revoke_old and prior_id and prior_id != new_key_id:
                del_resp = client.delete(f"{_API_ROOT}/api_keys/{prior_id}")
                sys.stderr.write(
                    f"sendgrid: DELETE /v3/api_keys/{prior_id} -> {del_resp.status_code}\n"
                )
                if del_resp.status_code not in (200, 202, 204):
                    raise ProducerContractError(
                        f"sendgrid producer: delete prior api_key failed "
                        f"({del_resp.status_code}): {_truncate(del_resp.text)}"
                    )

        return [Secret(name=key_entry.name, value=new_key_value)]
