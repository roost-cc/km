"""Twilio producer — rotate the auth token via Secondary → Promote.

Config block (``cfg.producers.twilio``):

- ``sid_secret``: name in current_secrets that holds the Account SID
  (default ``"text_userid"``).
- ``token_secret``: name in current_secrets that holds the current auth
  token (default ``"text_password"``).
- ``timeout_s``: HTTP timeout in seconds (default ``15.0``).

Promote is an implicit revoke (it leaves the prior token as the
secondary, which Twilio cycles on the next rotation), so ``revoke_old``
is unused here.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from typing import Any

import httpx

from km.producers.contract import Producer, ProducerContractError, Secret

_ACCOUNTS_ROOT = "https://accounts.twilio.com/v1"
_REST_ROOT = "https://api.twilio.com/2010-04-01"

_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")


def _http_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def _truncate(s: str, n: int = 200) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "..."


class Twilio(Producer):
    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self._sid_secret = config.get("sid_secret", "text_userid")
        self._token_secret = config.get("token_secret", "text_password")
        try:
            self._timeout = float(config.get("timeout_s", 15.0))
        except (TypeError, ValueError) as e:
            raise ProducerContractError("twilio producer: config.timeout_s must be a number") from e

    def produce(self, entries, ctx):
        if not entries:
            return []
        if len(entries) != 1:
            raise ProducerContractError(
                "twilio producer: rotation group must declare exactly one secret "
                "(the auth token); split immutable values like the Account SID "
                f"into a separate `producer: static` group. Got {len(entries)} entries."
            )
        token_entry = entries[0]

        sid = ctx.current_secrets.get(self._sid_secret)
        current_token = ctx.current_secrets.get(self._token_secret)
        if not isinstance(sid, str) or not sid:
            raise ProducerContractError(
                f"twilio producer: SID not found in current secrets "
                f"(looked at {self._sid_secret!r}); set `config.sid_secret` if the "
                "manifest uses a different name"
            )
        if not isinstance(current_token, str) or not current_token:
            raise ProducerContractError(
                f"twilio producer: current auth token not found in current "
                f"secrets (looked at {self._token_secret!r}); set "
                "`config.token_secret` if the manifest uses a different name"
            )
        if not _SID_RE.match(sid):
            raise ProducerContractError(
                f"twilio producer: SID {sid!r} does not match the documented "
                "shape `AC<32-hex>`; refusing to interpolate into request paths"
            )

        auth = (sid, current_token)

        with _http_client(self._timeout) as client:
            sec_url = f"{_ACCOUNTS_ROOT}/AuthTokens/Secondary"
            sec_resp = client.post(sec_url, auth=auth)
            sys.stderr.write(f"twilio: POST {sec_url} -> {sec_resp.status_code}\n")
            if sec_resp.status_code not in (200, 201):
                raise ProducerContractError(
                    f"twilio producer: secondary-create failed "
                    f"({sec_resp.status_code}): {_truncate(sec_resp.text)}"
                )
            sec_body = sec_resp.json()
            secondary_token = sec_body.get("secondary_auth_token")
            if not isinstance(secondary_token, str) or not secondary_token:
                raise ProducerContractError(
                    "twilio producer: secondary-create response missing `secondary_auth_token`"
                )

            prom_url = f"{_ACCOUNTS_ROOT}/AuthTokens/Promote"
            prom_resp = client.post(prom_url, auth=auth)
            sys.stderr.write(f"twilio: POST {prom_url} -> {prom_resp.status_code}\n")
            if prom_resp.status_code not in (200, 201):
                raise ProducerContractError(
                    f"twilio producer: promote failed "
                    f"({prom_resp.status_code}): {_truncate(prom_resp.text)}"
                )
            prom_body = prom_resp.json()
            new_primary = prom_body.get("auth_token")
            if not isinstance(new_primary, str) or not new_primary:
                raise ProducerContractError(
                    "twilio producer: promote response missing `auth_token`"
                )

        return [Secret(name=token_entry.name, value=new_primary)]
