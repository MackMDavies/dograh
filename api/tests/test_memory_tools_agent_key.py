"""The tool wizard authenticates with the ORG's own agent key, never the platform secret."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.memory_tools import router
from api.services.auth.depends import get_user

ACCOUNT = "3afb245c-400d-4178-8558-180d3faa644a"


def _client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(id=7, selected_organization_id=11, is_superuser=False)
    return TestClient(app)


def _db(provider_id=f"supabase_org_acct_{ACCOUNT}"):
    db = SimpleNamespace()
    db.get_organization_by_id = AsyncMock(return_value=SimpleNamespace(provider_id=provider_id))
    db.create_credential = AsyncMock(return_value=SimpleNamespace(credential_uuid="cred-1"))
    created = []

    async def create_tool(**kw):
        created.append(kw)
        return SimpleNamespace(tool_uuid=f"tool-{len(created)}")

    db.create_tool = create_tool
    db.get_tools_for_organization = AsyncMock(return_value=[])
    return db, created


def test_tools_carry_the_orgs_credential_not_a_secret():
    db, created = _db()
    with (
        patch("api.routes.memory_tools.db_client", db),
        patch("api.routes.memory_tools._mint_agent_key", new=AsyncMock(return_value={"key": "sak_x", "key_prefix": "sak_xxxxxxxx", "api_key_id": "k"})),
    ):
        resp = _client().post("/memory-tools/provision", json={"client_account_id": ACCOUNT})
    assert resp.status_code == 200
    assert len(created) == 7
    for tool in created:
        config = tool["definition"]["config"]
        assert config["credential_uuid"] == "cred-1"
        assert "headers" not in config
        assert "preset_parameters" not in config
    cred = db.create_credential.call_args.kwargs
    assert cred["credential_type"] == "custom_header"
    assert cred["credential_data"] == {"header_name": "x-sysevo-agent-key", "header_value": "sak_x"}


def test_refuses_another_organisations_account():
    db, created = _db()
    with patch("api.routes.memory_tools.db_client", db):
        resp = _client().post("/memory-tools/provision", json={"client_account_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 403
    assert created == []
    db.create_credential.assert_not_called()


def test_refuses_an_org_not_linked_to_an_account():
    db, _ = _db(provider_id="some-other-org")
    with patch("api.routes.memory_tools.db_client", db):
        resp = _client().post("/memory-tools/provision", json={"client_account_id": ACCOUNT})
    assert resp.status_code == 400
