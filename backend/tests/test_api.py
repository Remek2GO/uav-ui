"""The HTTP layer, mainly the checks that keep other web pages away from a drone."""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from uav_ui.main import create_app
from uav_ui.settings import Settings

REPO = Path(__file__).resolve().parents[2]
LOCAL = {"host": "127.0.0.1:8090"}


def plugin_zip(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.yaml", text)
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        schema_file=REPO / "spec" / "plugin.schema.json",
        static_dir=tmp_path / "no-frontend",
        plugin_dirs=[REPO / "plugins" / "example"],
        whep_url="http://127.0.0.1:9",
        video_sink_port=9,
        allowed_hosts={"127.0.0.1", "localhost"},
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8090") as test_client:
        yield test_client


def test_works_with_no_drone_at_all(client):
    assert client.get("/api/status").json()["profile"] is None
    assert len(client.get("/api/profiles").json()) == 2
    assert client.get("/api/map/sources").status_code == 200


def test_foreign_host_is_refused(client):
    assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 403


def test_cross_site_post_is_refused(client):
    response = client.post(
        "/api/flight/rtl", json={"confirmed": True}, headers={"origin": "http://evil.example"}
    )
    assert response.status_code == 403


def test_same_origin_post_is_allowed(client):
    response = client.post(
        "/api/flight/rtl", json={"confirmed": True}, headers={"origin": "http://127.0.0.1:8090"}
    )
    assert response.status_code == 200 and response.json()["status"] == "no_vehicle"


def test_cross_site_upload_is_refused(client):
    response = client.post(
        "/api/plugins",
        files={"file": ("p.zip", plugin_zip("api: 1\nid: x\nname: X\n"))},
        headers={"origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert all(p["id"] != "x" for p in client.get("/api/plugins").json()["plugins"])


def test_websocket_from_another_site_is_refused(client):
    headers = {**LOCAL, "origin": "http://evil.example"}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=headers):
            pass


def test_websocket_with_a_foreign_host_is_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"host": "evil.example"}):
            pass


def test_websocket_sends_a_snapshot(client):
    headers = {**LOCAL, "origin": "http://127.0.0.1:8090"}
    with client.websocket_connect("/ws", headers=headers) as socket:
        assert socket.receive_json()["type"] == "snapshot"


def test_flight_command_requires_confirmation(client):
    assert client.post("/api/flight/rtl", json={}).status_code == 400
    assert client.post("/api/flight/rtl", json={"confirmed": False}).status_code == 400


def test_plugin_upload_list_delete(client):
    response = client.post("/api/plugins", files={"file": ("p.zip", plugin_zip("api: 1\nid: x\nname: X\n"))})
    assert response.status_code == 200
    ids = {p["id"]: p["origin"] for p in client.get("/api/plugins").json()["plugins"]}
    assert ids == {"x": "uploaded", "example": "directory"}
    assert client.delete("/api/plugins/x").status_code == 200
    assert client.delete("/api/plugins/example").status_code == 404  # directories are not deleted


def test_invalid_plugin_upload_gives_a_reason(client):
    response = client.post("/api/plugins", files={"file": ("p.zip", plugin_zip("api: 1\nid: x\n"))})
    assert response.status_code == 400 and "name" in response.json()["detail"]


def test_command_confirmation_is_enforced_by_the_server(client):
    response = client.post("/api/plugins/example/commands/latched", json={"args": {"color": "red"}})
    assert response.status_code == 400


def test_command_without_connection_is_reported(client):
    response = client.post("/api/plugins/example/commands/say", json={"args": {"text": "x"}})
    assert response.json()["status"] == "no_bridge"


class _FakeBridge:
    online = True

    def __init__(self, reply):
        self.reply = reply

    async def run_command(self, _name, _args):
        return self.reply

    def status(self):
        return {"url": "fake", "online": True, "error": None, "bytes_per_s": 0, "info": None}

    async def stop(self):
        pass


def _last_event(client):
    return list(client.app.state.session.events)[-1]


def test_refusal_from_a_service_is_logged_as_a_refusal(client):
    client.app.state.session.bridge = _FakeBridge(
        {"status": "ok", "result": {"success": False, "message": "misja trwa"}}
    )
    response = client.post("/api/plugins/example/commands/add", json={"args": {"a": 1, "b": 2}})
    assert response.json()["result"]["success"] is False
    event = _last_event(client)
    assert event["level"] == "warn" and event["text"].endswith("odmowa")


def test_success_from_a_service_is_logged_as_ok(client):
    client.app.state.session.bridge = _FakeBridge({"status": "ok", "result": {"sum": 3}})
    client.post("/api/plugins/example/commands/add", json={"args": {"a": 1, "b": 2}})
    event = _last_event(client)
    assert event["level"] == "info" and event["text"].endswith("ok")


def test_unknown_command_and_plugin(client):
    assert client.post("/api/plugins/example/commands/nope", json={}).status_code == 404
    assert client.post("/api/plugins/nope/commands/x", json={}).status_code == 404


def test_custom_panel_is_served_sandboxed_with_no_network(client):
    response = client.get("/plugin-assets/example/panel.html")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp and "default-src 'none'" in csp
    assert "allow-same-origin" not in csp
    assert client.get("/plugin-assets/example/..%2Fplugin.yaml").status_code == 404


def test_plans_api(client):
    plan = {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [19.9, 50.0]}}
    assert client.put("/api/plans/example/punkt", json=plan).status_code == 200
    assert client.get("/api/plans/example").json() == [{"name": "punkt", "plan": plan}]
    assert client.put("/api/plans/example/zly", json={"type": "x"}).status_code == 400
    assert client.delete("/api/plans/example/punkt").status_code == 200


def test_connect_with_a_busy_port_explains_itself(client):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        client.put("/api/profiles", json=[{"name": "p", "mavlink": {"listen": port}}])
        response = client.post("/api/connect", json={"name": "p"})
        assert response.status_code == 400 and "portu" in response.json()["detail"]
    assert client.get("/api/status").json()["profile"] is None


def test_map_only_profile_connects(client):
    client.put("/api/profiles", json=[{"name": "mapa"}])
    assert client.post("/api/connect", json={"name": "mapa"}).status_code == 200
    assert client.get("/api/status").json()["profile"]["name"] == "mapa"
    assert client.post("/api/disconnect").status_code == 200
