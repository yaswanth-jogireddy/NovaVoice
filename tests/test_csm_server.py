"""HTTP contracts for the CSM routes: flags enforced on the server, admin and staff tokens."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

import features
from csm import orchestrator
from features import FeatureFlags


@pytest.fixture()
def app_with(seeded_db, tmp_path, monkeypatch):
    import demo_server

    def build(env):
        monkeypatch.setattr(features, "_flags", FeatureFlags(env))
        monkeypatch.setattr(orchestrator, "_services", orchestrator.Services(seeded_db))
        # an aiohttp app is bound to one event loop: build a fresh one per request
        return lambda: demo_server.create_app(port=0, trace_path=tmp_path / "trace.sqlite3")
    return build


def call(make_app, method, path, **kw):
    async def go():
        app = make_app()
        server = TestServer(app, host="127.0.0.1")
        client = TestClient(server)
        await client.start_server()
        app["hosts"] = {f"127.0.0.1:{server.port}"}
        try:
            resp = await client.request(method, path, **kw)
            body = await resp.text()
            return resp.status, body
        finally:
            await client.close()
    return asyncio.run(go())


def test_config_reflects_milestone(app_with):
    status, body = call(app_with({"MILESTONE": "1"}), "GET", "/api/config")
    assert status == 200 and '"milestone": 1' in body and '"ACCOUNT_TOOLS": true' in body and '"WARM_HANDOFF": false' in body


def test_m1_hides_desk_and_metrics(app_with):
    app = app_with({"MILESTONE": "1"})
    assert call(app, "GET", "/api/handoffs")[0] == 404
    assert call(app, "GET", "/api/metrics")[0] == 404


def test_m3_serves_desk_and_metrics(app_with):
    app = app_with({"MILESTONE": "3"})
    assert call(app, "GET", "/api/handoffs")[0] == 200
    status, body = call(app, "GET", "/api/metrics")
    assert status == 200 and "containment_rate" in body and "human_cost_is_assumption" in body


def test_staff_token_required_when_set(app_with, monkeypatch):
    monkeypatch.setenv("STAFF_TOKEN", "s3cret-staff")
    app = app_with({"MILESTONE": "3"})
    assert call(app, "GET", "/api/metrics")[0] == 401
    assert call(app, "GET", "/api/metrics", headers={"X-Staff-Token": "s3cret-staff"})[0] == 200


def test_milestone_switch_needs_admin_token(app_with):
    app = app_with({"MILESTONE": "1", "ADMIN_TOKEN": "a-long-admin-token"})
    assert call(app, "POST", "/api/admin/milestone", json={"milestone": 2})[0] == 403
    status, body = call(app, "POST", "/api/admin/milestone", json={"milestone": 2},
                        headers={"X-Admin-Token": "a-long-admin-token"})
    assert status == 200 and '"milestone": 2' in body
    assert features.flags().enabled("WARM_HANDOFF")


def test_language_gated_by_multilingual_flag(app_with):
    status, _ = call(app_with({"MILESTONE": "1"}), "GET", "/api/session?language=hi&mode=text")
    assert status == 400


def test_evals_endpoint_gated(app_with):
    assert call(app_with({"MILESTONE": "2"}), "GET", "/api/evals")[0] == 404
    status, body = call(app_with({"MILESTONE": "3"}), "GET", "/api/evals")
    assert status == 200 and "guardrail_pass_rate" in body and '"said"' not in body


@pytest.mark.parametrize("path", ["/", "/desk", "/supervisor", "/assets/call.js", "/assets/shared.css", "/assets/capture.js"])
def test_ui_pages_and_assets_served(app_with, path):
    assert call(app_with({"MILESTONE": "1"}), "GET", path)[0] == 200


def test_unknown_asset_refused(app_with):
    assert call(app_with({"MILESTONE": "1"}), "GET", "/assets/demo_server.py")[0] == 404


def test_metrics_carry_trust_pillars(app_with):
    _, body = call(app_with({"MILESTONE": "3"}), "GET", "/api/metrics")
    for pillar in ("Identity & SSO", "Role-Based Access", "Auditability", "Data Protection", "Human Oversight", "Secure Deployment"):
        assert pillar in body


def test_mock_customer_routes(app_with):
    app = app_with({"MILESTONE": "2"})
    status, body = call(app, "POST", "/api/mock-customers", json={"full_name": "Test Person", "dob": "1990-06-15",
                        "mobile": "9876500999", "account_last4": "1234", "card_last4": "5678", "balance_inr": 1000})
    assert status == 201 and '"customer_id": "C9001"' in body
    assert call(app, "POST", "/api/mock-customers", json={"dob": "nope"})[0] == 400
    assert "C9001" in call(app, "GET", "/api/test-customers")[1]
    assert call(app_with({"MILESTONE": "1", "FEATURE_ACCOUNT_TOOLS": "off"}), "POST", "/api/mock-customers", json={})[0] == 404


def test_mock_customer_needs_staff_token_when_set(app_with, monkeypatch):
    monkeypatch.setenv("STAFF_TOKEN", "s3cret-staff")
    assert call(app_with({"MILESTONE": "3"}), "GET", "/api/mock-customers/defaults")[0] == 401


def test_voice_clone_is_stored_and_offered(app_with, monkeypatch, tmp_path):
    import aiohttp
    import httpx
    from csm import voices
    monkeypatch.setattr(voices, "CLONES", tmp_path / "cloned.json")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"voice_id": "EL_CLONED_42", "requires_verification": False}

    async def fake_post(self, url, **kw):
        assert url.endswith("/v1/voices/add") and kw["headers"]["xi-api-key"]
        return FakeResponse()
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    def form(consent):
        data = aiohttp.FormData()
        data.add_field("name", "Abhi")
        data.add_field("consent", consent)
        data.add_field("sample", b"RIFF0000WAVE", filename="me.wav", content_type="audio/wav")
        return data
    app = app_with({"MILESTONE": "3"})
    assert call(app, "POST", "/api/voice-clone", data=form("false"))[0] == 400      # consent required
    status, body = call(app, "POST", "/api/voice-clone", data=form("true"))
    assert status == 200 and "clone-abhi" in body
    assert "Abhi (cloned)" in call(app, "GET", "/api/voices")[1]
