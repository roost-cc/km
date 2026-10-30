"""Unit tests for km.infisical: InfisicalClient (ports infisical.unit.test.js) and InfisicalAdmin."""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from km.infisical import Identity, InfisicalAdmin, InfisicalClient


@pytest.fixture
def client() -> InfisicalClient:
    return InfisicalClient(
        host="http://infisical.test",
        token="test-token",
        project_id="proj-123",
        environment="dev",
    )


# --- _parse_secret ---


def test_parse_secret_has_no_tags_field(client: InfisicalClient) -> None:
    parsed = InfisicalClient._parse_secret(
        {
            "secretKey": "foo",
            "secretPath": "/bar",
            "secretValue": "baz",
            "updatedAt": "2026-04-21T00:00:00Z",
            "secretComment": "hi",
            "tags": [{"name": "mint=roost"}],
        }
    )
    assert parsed.name == "foo"
    assert parsed.path == "/bar"
    assert parsed.value == "baz"
    assert parsed.metadata.updated_at == "2026-04-21T00:00:00Z"
    assert parsed.metadata.comment == "hi"


# --- create_folder ---


def test_create_folder_root_is_noop(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    client.create_folder("/")
    assert len(httpx_mock.get_requests()) == 0


def test_create_folder_rejects_relative(client: InfisicalClient) -> None:
    with pytest.raises(TypeError, match="must be an absolute"):
        client.create_folder("relative/path")


def test_create_folder_multi_segment(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={"folder": {}})
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={"folder": {}})

    client.create_folder("/oauth_client/apple")

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    import json

    body1 = json.loads(reqs[0].content)
    assert body1 == {
        "workspaceId": "proj-123",
        "environment": "dev",
        "directory": "/",
        "name": "oauth_client",
    }
    body2 = json.loads(reqs[1].content)
    assert body2 == {
        "workspaceId": "proj-123",
        "environment": "dev",
        "directory": "/oauth_client",
        "name": "apple",
    }


def test_create_folder_409_already_exists_is_idempotent(
    client: InfisicalClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders",
        status_code=409,
        text="Folder already exists",
    )
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={})
    client.create_folder("/jwt/account")
    assert len(httpx_mock.get_requests()) == 2


def test_create_folder_400_already_exists_is_idempotent(
    client: InfisicalClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders",
        status_code=400,
        text="Folder with name 'oauth_client' already exists in path '/'",
    )
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={})
    client.create_folder("/oauth_client/apple")
    assert len(httpx_mock.get_requests()) == 2


def test_create_folder_403_propagates(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders",
        status_code=403,
        text="Forbidden",
    )
    with pytest.raises(RuntimeError, match="403.*Forbidden"):
        client.create_folder("/jwt")


def test_create_folder_400_non_idempotent_propagates(
    client: InfisicalClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders",
        status_code=400,
        text="Invalid folder name",
    )
    with pytest.raises(RuntimeError, match="400.*Invalid folder name"):
        client.create_folder("/bad")


def test_create_folder_single_segment(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={})
    client.create_folder("/jwt")
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 1
    import json

    body = json.loads(reqs[0].content)
    assert body["directory"] == "/"
    assert body["name"] == "jwt"


# --- list_folders ---


def test_list_folders_normalizes_id_fields(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders?workspaceId=proj-123&environment=dev&path=%2F",
        json={
            "folders": [
                {"id": "f1", "name": "jwt"},
                {"_id": "f2", "name": "encryption"},
            ]
        },
    )
    result = client.list_folders("/")
    assert result == [{"id": "f1", "name": "jwt"}, {"id": "f2", "name": "encryption"}]


def test_list_folders_error(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders?workspaceId=proj-123&environment=dev&path=%2F",
        status_code=500,
        text="boom",
    )
    with pytest.raises(RuntimeError, match="500.*boom"):
        client.list_folders("/")


# --- delete_folder ---


def test_delete_folder_rejects_root(client: InfisicalClient) -> None:
    with pytest.raises(TypeError, match="absolute non-root"):
        client.delete_folder("/")
    with pytest.raises(TypeError, match="absolute non-root"):
        client.delete_folder("relative")


def test_delete_folder_lookup_then_delete(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders?workspaceId=proj-123&environment=dev&path=%2F",
        json={"folders": [{"id": "f-jwt", "name": "jwt"}]},
    )
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders/f-jwt",
        method="DELETE",
        json={},
    )
    client.delete_folder("/jwt")
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    assert reqs[1].method == "DELETE"
    import json

    body = json.loads(reqs[1].content)
    assert body == {"workspaceId": "proj-123", "environment": "dev", "path": "/"}


def test_delete_folder_nested_parent_path(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders?workspaceId=proj-123&environment=dev&path=%2Foauth_client",
        json={"folders": [{"id": "f-apple", "name": "apple"}]},
    )
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders/f-apple",
        method="DELETE",
        json={},
    )
    client.delete_folder("/oauth_client/apple")
    reqs = httpx_mock.get_requests()
    import json

    body = json.loads(reqs[1].content)
    assert body["path"] == "/oauth_client"


def test_delete_folder_missing_is_noop(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders?workspaceId=proj-123&environment=dev&path=%2F",
        json={"folders": []},
    )
    client.delete_folder("/ghost")
    assert len(httpx_mock.get_requests()) == 1


def test_delete_folder_404_on_delete_idempotent(
    client: InfisicalClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders?workspaceId=proj-123&environment=dev&path=%2F",
        json={"folders": [{"id": "f", "name": "ghost"}]},
    )
    httpx_mock.add_response(
        url="http://infisical.test/api/v1/folders/f",
        method="DELETE",
        status_code=404,
        text="gone",
    )
    client.delete_folder("/ghost")


# --- set ---


def test_set_missing_secret_creates_folder_chain(
    client: InfisicalClient, httpx_mock: HTTPXMock
) -> None:
    # get() — 404
    httpx_mock.add_response(
        url="http://infisical.test/api/v3/secrets/raw/client_id?workspaceId=proj-123&environment=dev&secretPath=%2Foauth_client%2Fapple",
        status_code=404,
        text="not found",
    )
    # createFolder: oauth_client
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={})
    # createFolder: apple
    httpx_mock.add_response(url="http://infisical.test/api/v1/folders", json={})
    # POST secret
    httpx_mock.add_response(
        url="http://infisical.test/api/v3/secrets/raw/client_id",
        method="POST",
        json={},
    )

    client.set("client_id", "value", "/oauth_client/apple")

    reqs = httpx_mock.get_requests()
    assert len(reqs) == 4
    assert "/api/v1/folders" in str(reqs[1].url)
    assert "/api/v1/folders" in str(reqs[2].url)
    assert reqs[3].method == "POST"


def test_set_existing_secret_patches_without_createfolder(
    client: InfisicalClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v3/secrets/raw/n?workspaceId=proj-123&environment=dev&secretPath=%2Fjwt%2Faccount",
        json={"secret": {"secretKey": "n", "secretValue": "v", "updatedAt": ""}},
    )
    httpx_mock.add_response(
        url="http://infisical.test/api/v3/secrets/raw/n",
        method="PATCH",
        json={},
    )
    client.set("n", "new-value", "/jwt/account")
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    assert all("/api/v1/folders" not in str(r.url) for r in reqs)
    assert reqs[1].method == "PATCH"


def test_set_at_root_skips_createfolder(client: InfisicalClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://infisical.test/api/v3/secrets/raw/root_secret?workspaceId=proj-123&environment=dev&secretPath=%2F",
        status_code=404,
        text="not found",
    )
    httpx_mock.add_response(
        url="http://infisical.test/api/v3/secrets/raw/root_secret",
        method="POST",
        json={},
    )
    client.set("root_secret", "v", "/")
    reqs = httpx_mock.get_requests()
    assert len(reqs) == 2
    assert all("/api/v1/folders" not in str(r.url) for r in reqs)


# --- InfisicalAdmin ---

H = "http://infisical.test"


@pytest.fixture
def admin() -> InfisicalAdmin:
    return InfisicalAdmin(host=H + "/", token="admin-tok")


def _body(req) -> dict:
    return json.loads(req.content)


def test_admin_without_token_sends_no_authorization(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{H}/api/v1/admin/config", json={"config": {"initialized": False}})
    with InfisicalAdmin(host=H) as anon:
        assert anon.is_initialized() is False
    assert "authorization" not in httpx_mock.get_requests()[0].headers


def test_admin_is_initialized(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{H}/api/v1/admin/config", json={"config": {"initialized": True}})
    assert admin.is_initialized() is True
    assert httpx_mock.get_requests()[0].headers["authorization"] == "Bearer admin-tok"


def test_admin_bootstrap(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/admin/bootstrap",
        method="POST",
        json={
            "message": "Successfully bootstrapped instance",
            "user": {"email": "a@example.com"},
            "organization": {"id": "org-1", "name": "Roost"},
            "identity": {"id": "i-1", "credentials": {"token": "jwt-1"}},
        },
    )
    boot = admin.bootstrap(email="a@example.com", password="pw", organization="Roost")
    assert (boot.org_id, boot.token) == ("org-1", "jwt-1")
    assert _body(httpx_mock.get_requests()[0]) == {
        "email": "a@example.com",
        "password": "pw",
        "organization": "Roost",
    }


def test_admin_bootstrap_twice_raises(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/admin/bootstrap",
        method="POST",
        status_code=400,
        json={"message": "Instance has already been set up"},
    )
    with pytest.raises(RuntimeError, match="bootstrap the instance: 400"):
        admin.bootstrap(email="a", password="p", organization="o")


def test_admin_create_project(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v2/workspace", method="POST", json={"project": {"id": "p-1"}}
    )
    assert admin.create_project("roost") == "p-1"
    assert _body(httpx_mock.get_requests()[0]) == {
        "projectName": "roost",
        "type": "secret-manager",
    }


def test_admin_list_and_create_environments(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/workspace/p-1",
        json={"workspace": {"environments": [{"slug": "prod"}, {"slug": "dev"}]}},
    )
    httpx_mock.add_response(
        url=f"{H}/api/v1/workspace/p-1/environments", method="POST", json={"environment": {}}
    )
    assert admin.list_environments("p-1") == ["dev", "prod"]
    admin.create_environment("p-1", "test")
    assert _body(httpx_mock.get_requests()[1]) == {"name": "test", "slug": "test"}


def test_admin_create_environment_conflict_raises(
    admin: InfisicalAdmin, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/workspace/p-1/environments",
        method="POST",
        status_code=400,
        json={"message": "Environment with slug already exists"},
    )
    with pytest.raises(RuntimeError, match='create environment "dev": 400'):
        admin.create_environment("p-1", "dev")


def test_admin_list_identities(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v2/organizations/org-1/identity-memberships?limit=1000",
        json={
            "identityMemberships": [
                {
                    "identityId": "i-1",
                    "identity": {"id": "i-1", "name": "km", "authMethods": ["token-auth"]},
                },
                {"identityId": "i-2", "identity": {"id": "i-2", "name": "services"}},
            ]
        },
    )
    assert admin.list_identities("org-1") == [
        Identity(id="i-1", name="km", auth_methods=["token-auth"]),
        Identity(id="i-2", name="services", auth_methods=[]),
    ]


def test_admin_create_identity(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/identities", method="POST", json={"identity": {"id": "i-9"}}
    )
    assert admin.create_identity("services", "org-1") == "i-9"
    assert _body(httpx_mock.get_requests()[0]) == {
        "name": "services",
        "organizationId": "org-1",
        "role": "no-access",
    }


def test_admin_attach_token_auth(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/auth/token-auth/identities/i-9",
        method="POST",
        json={"identityTokenAuth": {}},
    )
    admin.attach_token_auth("i-9")
    body = _body(httpx_mock.get_requests()[0])
    assert (body["accessTokenTTL"], body["accessTokenMaxTTL"], body["accessTokenNumUsesLimit"]) == (
        0,
        0,
        0,
    )


def test_admin_create_token(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v1/auth/token-auth/identities/i-9/tokens",
        method="POST",
        json={"accessToken": "jwt-9", "expiresIn": 0},
    )
    assert admin.create_token("i-9", "km-local") == "jwt-9"
    assert _body(httpx_mock.get_requests()[0]) == {"name": "km-local"}


def test_admin_project_identities_and_add(admin: InfisicalAdmin, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{H}/api/v2/workspace/p-1/identity-memberships",
        method="GET",
        json={"identityMemberships": [{"identityId": "i-1", "roles": [{"role": "admin"}]}]},
    )
    httpx_mock.add_response(
        url=f"{H}/api/v2/workspace/p-1/identity-memberships/i-9",
        method="POST",
        json={"identityMembership": {}},
    )
    assert admin.project_identity_roles("p-1") == {"i-1": ["admin"]}
    admin.add_identity_to_project("p-1", "i-9", "viewer")
    assert _body(httpx_mock.get_requests()[1]) == {"role": "viewer"}
