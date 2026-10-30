"""Infisical HTTP client.

Port of `src/adapters/infisical.js` — matches the post-S-305 public surface
(no tag methods). Uses `httpx.Client` for synchronous connection-pooled
HTTP — the CLI is linear so there's no throughput gain from asyncio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import httpx

ALREADY_EXISTS_RE = re.compile(r"already exists", re.IGNORECASE)


@dataclass
class SecretMetadata:
    updated_at: str
    comment: str


@dataclass
class SecretInfo:
    name: str
    path: str
    value: str
    metadata: SecretMetadata


class InfisicalClient:
    """Synchronous Infisical HTTP client.

    Host/token/project/environment are fixed at construction; methods
    accept only the per-call identifying data.
    """

    def __init__(
        self,
        *,
        host: str,
        token: str,
        project_id: str,
        environment: str,
    ) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self.project_id = project_id
        self.environment = environment
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> InfisicalClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        full = urljoin(self.host + "/", path.lstrip("/"))
        if params:
            clean = {k: str(v) for k, v in params.items() if v is not None}
            if clean:
                full += "?" + urlencode(clean)
        return full

    @staticmethod
    def _parse_secret(raw: dict[str, Any]) -> SecretInfo:
        return SecretInfo(
            name=raw["secretKey"],
            path=raw.get("secretPath") or "/",
            value=raw.get("secretValue") or "",
            metadata=SecretMetadata(
                updated_at=raw.get("updatedAt") or "",
                comment=raw.get("secretComment") or "",
            ),
        )

    # --- secret CRUD ---

    def list(self, path: str, *, recursive: bool = False) -> list[SecretInfo]:
        url = self._url(
            "/api/v3/secrets/raw",
            {
                "workspaceId": self.project_id,
                "environment": self.environment,
                "secretPath": path,
                "recursive": "true" if recursive else "false",
            },
        )
        res = self._client.get(url)
        if res.status_code >= 400:
            raise RuntimeError(f"Failed to list secrets: {res.status_code} {res.text}")
        data = res.json()
        return [self._parse_secret(s) for s in data.get("secrets", [])]

    def get(self, name: str, path: str) -> SecretInfo | None:
        url = self._url(
            f"/api/v3/secrets/raw/{quote(name, safe='')}",
            {
                "workspaceId": self.project_id,
                "environment": self.environment,
                "secretPath": path,
            },
        )
        res = self._client.get(url)
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise RuntimeError(f'Failed to get secret "{name}": {res.status_code} {res.text}')
        data = res.json()
        return self._parse_secret(data["secret"])

    def set(
        self,
        name: str,
        value: str,
        path: str,
        *,
        comment: str | None = None,
    ) -> None:
        existing = self.get(name, path)
        endpoint = f"/api/v3/secrets/raw/{quote(name, safe='')}"
        body: dict[str, Any] = {
            "workspaceId": self.project_id,
            "environment": self.environment,
            "secretPath": path,
            "secretValue": value,
        }
        if comment is not None:
            body["secretComment"] = comment

        if existing is not None:
            res = self._client.patch(self._url(endpoint), json=body)
            if res.status_code >= 400:
                raise RuntimeError(
                    f'Failed to update secret "{name}": {res.status_code} {res.text}'
                )
        else:
            if path != "/":
                self.create_folder(path)
            body["type"] = "shared"
            res = self._client.post(self._url(endpoint), json=body)
            if res.status_code >= 400:
                raise RuntimeError(
                    f'Failed to create secret "{name}": {res.status_code} {res.text}'
                )

    def delete(self, name: str, path: str) -> None:
        endpoint = f"/api/v3/secrets/raw/{quote(name, safe='')}"
        res = self._client.request(
            "DELETE",
            self._url(endpoint),
            json={
                "workspaceId": self.project_id,
                "environment": self.environment,
                "secretPath": path,
            },
        )
        if res.status_code == 404:
            return
        if res.status_code >= 400:
            raise RuntimeError(f'Failed to delete secret "{name}": {res.status_code} {res.text}')

    # --- workspace metadata ---

    def list_environments(self) -> list[str]:
        """Return the workspace's environment slugs (e.g. ['dev', 'prod']).

        Doesn't depend on ``self.environment`` — workspace metadata is
        global. Sorted for stable output.
        """
        url = self._url(f"/api/v1/workspace/{quote(self.project_id, safe='')}")
        res = self._client.get(url)
        if res.status_code >= 400:
            raise RuntimeError(
                f"Failed to list workspace environments: {res.status_code} {res.text}"
            )
        data = res.json()
        workspace = data.get("workspace") or data
        envs = workspace.get("environments") or []
        slugs: list[str] = []
        for e in envs:
            if isinstance(e, dict):
                slug = e.get("slug") or e.get("name")
                if isinstance(slug, str) and slug:
                    slugs.append(slug)
        return sorted(slugs)

    # --- folder CRUD ---

    def create_folder(self, path: str) -> None:
        """Create a folder (and any missing ancestors) at ``path``. Idempotent.

        Treats 400/409 "already exists" as success so speculative calls are
        safe. `/` is a no-op.
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise TypeError("path must be an absolute string starting with /")
        segments = [s for s in path.split("/") if s]
        directory = "/"
        for name in segments:
            res = self._client.post(
                self._url("/api/v1/folders"),
                json={
                    "workspaceId": self.project_id,
                    "environment": self.environment,
                    "directory": directory,
                    "name": name,
                },
            )
            if res.status_code >= 400:
                body = res.text
                already = (
                    res.status_code in (400, 409) and ALREADY_EXISTS_RE.search(body) is not None
                )
                if not already:
                    full = f"{directory}{'' if directory.endswith('/') else '/'}{name}"
                    raise RuntimeError(
                        f'Failed to create folder "{full}": {res.status_code} {body}'
                    )
            directory = f"/{name}" if directory == "/" else f"{directory}/{name}"

    def list_folders(self, path: str) -> list[dict[str, str]]:
        url = self._url(
            "/api/v1/folders",
            {
                "workspaceId": self.project_id,
                "environment": self.environment,
                "path": path,
            },
        )
        res = self._client.get(url)
        if res.status_code >= 400:
            raise RuntimeError(f'Failed to list folders at "{path}": {res.status_code} {res.text}')
        data = res.json()
        return [
            {"id": f.get("id") or f.get("_id"), "name": f["name"]} for f in data.get("folders", [])
        ]

    def delete_folder(self, path: str) -> None:
        """Delete the folder at ``path``. Idempotent: missing folder is success."""
        if not isinstance(path, str) or not path.startswith("/") or path == "/":
            raise TypeError("path must be an absolute non-root string starting with /")
        idx = path.rfind("/")
        parent = "/" if idx == 0 else path[:idx]
        name = path[idx + 1 :]

        siblings = self.list_folders(parent)
        match = next((f for f in siblings if f["name"] == name), None)
        if match is None:
            return  # already gone

        res = self._client.request(
            "DELETE",
            self._url(f"/api/v1/folders/{quote(str(match['id']), safe='')}"),
            json={
                "workspaceId": self.project_id,
                "environment": self.environment,
                "path": parent,
            },
        )
        if res.status_code == 404:
            return
        if res.status_code >= 400:
            raise RuntimeError(f'Failed to delete folder "{path}": {res.status_code} {res.text}')


@dataclass
class Bootstrap:
    """What ``POST /api/v1/admin/bootstrap`` returns that km keeps."""

    org_id: str
    token: str  # the instance admin identity's Token Auth token


@dataclass
class Identity:
    id: str
    name: str
    auth_methods: list[str]


class InfisicalAdmin:
    """Instance and organisation calls: no fixed project or environment.

    ``token`` is optional because the two instance-setup calls
    (:meth:`is_initialized`, :meth:`bootstrap`) are unauthenticated; every
    other call needs an identity allowed to create projects and
    identities in the organisation (the bootstrap identity) or, for the
    project calls, a project admin.
    """

    def __init__(self, *, host: str, token: str | None = None) -> None:
        self.host = host.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(headers=headers, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> InfisicalAdmin:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return urljoin(self.host + "/", path.lstrip("/"))

    def _call(self, method: str, path: str, what: str, **kw: Any) -> dict[str, Any]:
        res = self._client.request(method, self._url(path), **kw)
        if res.status_code >= 400:
            raise RuntimeError(f"Failed to {what}: {res.status_code} {res.text}")
        return res.json()

    # --- instance ---

    def is_initialized(self) -> bool:
        """Whether the instance has an admin (``GET /api/v1/admin/config``)."""
        data = self._call("GET", "/api/v1/admin/config", "read the instance config")
        return bool((data.get("config") or {}).get("initialized"))

    def bootstrap(self, *, email: str, password: str, organization: str) -> Bootstrap:
        """Create the instance's admin user, organisation and admin identity.

        Infisical answers 400 "already been set up" on a second call; km
        checks :meth:`is_initialized` first.
        """
        data = self._call(
            "POST",
            "/api/v1/admin/bootstrap",
            "bootstrap the instance",
            json={"email": email, "password": password, "organization": organization},
        )
        return Bootstrap(
            org_id=data["organization"]["id"],
            token=data["identity"]["credentials"]["token"],
        )

    # --- projects and environments ---

    def create_project(self, name: str) -> str:
        """Create a secret-manager project; return its id."""
        data = self._call(
            "POST",
            "/api/v2/workspace",
            f'create project "{name}"',
            json={"projectName": name, "type": "secret-manager"},
        )
        return data["project"]["id"]

    def list_environments(self, project_id: str) -> list[str]:
        """The project's environment slugs, sorted."""
        data = self._call(
            "GET",
            f"/api/v1/workspace/{quote(project_id, safe='')}",
            "list project environments",
        )
        envs = (data.get("workspace") or {}).get("environments") or []
        return sorted(e["slug"] for e in envs if isinstance(e, dict) and e.get("slug"))

    def create_environment(self, project_id: str, slug: str) -> None:
        self._call(
            "POST",
            f"/api/v1/workspace/{quote(project_id, safe='')}/environments",
            f'create environment "{slug}"',
            json={"name": slug, "slug": slug},
        )

    # --- machine identities ---

    def list_identities(self, org_id: str) -> list[Identity]:
        data = self._call(
            "GET",
            f"/api/v2/organizations/{quote(org_id, safe='')}/identity-memberships",
            "list machine identities",
            params={"limit": 1000},
        )
        out: list[Identity] = []
        for m in data.get("identityMemberships") or []:
            ident = m.get("identity") or {}
            out.append(
                Identity(
                    id=ident.get("id") or m.get("identityId"),
                    name=ident.get("name") or "",
                    auth_methods=list(ident.get("authMethods") or []),
                )
            )
        return out

    def create_identity(self, name: str, org_id: str) -> str:
        """Create an organisation identity with no organisation access
        (its access comes from project membership); return its id."""
        data = self._call(
            "POST",
            "/api/v1/identities",
            f'create machine identity "{name}"',
            json={"name": name, "organizationId": org_id, "role": "no-access"},
        )
        return data["identity"]["id"]

    def attach_token_auth(self, identity_id: str) -> None:
        """Attach Token Auth with non-expiring, unlimited-use tokens."""
        self._call(
            "POST",
            f"/api/v1/auth/token-auth/identities/{quote(identity_id, safe='')}",
            "attach Token Auth",
            json={
                "accessTokenTTL": 0,
                "accessTokenMaxTTL": 0,
                "accessTokenNumUsesLimit": 0,
                "accessTokenTrustedIps": [{"ipAddress": "0.0.0.0/0"}, {"ipAddress": "::/0"}],
            },
        )

    def create_token(self, identity_id: str, name: str) -> str:
        """Mint a Token Auth token for the identity; return the JWT."""
        data = self._call(
            "POST",
            f"/api/v1/auth/token-auth/identities/{quote(identity_id, safe='')}/tokens",
            "create a Token Auth token",
            json={"name": name},
        )
        return data["accessToken"]

    def project_identity_roles(self, project_id: str) -> dict[str, list[str]]:
        """identity id → its role slugs in the project."""
        data = self._call(
            "GET",
            f"/api/v2/workspace/{quote(project_id, safe='')}/identity-memberships",
            "list project identities",
        )
        out: dict[str, list[str]] = {}
        for m in data.get("identityMemberships") or []:
            out[m["identityId"]] = [r.get("role") for r in m.get("roles") or []]
        return out

    def add_identity_to_project(self, project_id: str, identity_id: str, role: str) -> None:
        self._call(
            "POST",
            f"/api/v2/workspace/{quote(project_id, safe='')}/identity-memberships/"
            f"{quote(identity_id, safe='')}",
            f'add identity to the project as "{role}"',
            json={"role": role},
        )
