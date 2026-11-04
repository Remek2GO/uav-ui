"""uav-ui backend: REST and WebSocket API, plus the built frontend."""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from .plans import GLOBAL_SCOPE, PlanError, PlanStore
from .plugins import MAX_UNPACKED_BYTES, PluginStore, PluginValidationError
from .session import ProfileError, Session
from .settings import Settings
from .tiles import TileCache, TileError

# A sandboxed page with an opaque origin and no network at all. Plugin JS talks only to the parent
# window, which applies the same whitelist and confirmations as the rest of the GUI.
CUSTOM_PANEL_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data:; font-src data:"
)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_name(value):
    return urlsplit(f"//{value}").hostname if value else None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    plugins = PluginStore(settings.data_dir, settings.schema_file, settings.plugin_dirs)
    plans = PlanStore(settings.data_dir)
    tiles = TileCache(settings.data_dir)
    session = Session(settings, plugins)
    whep = httpx.AsyncClient(timeout=10, trust_env=False)

    @asynccontextmanager
    async def lifespan(_app):
        await tiles.start()
        await session.start()
        yield
        await session.stop()
        await tiles.stop()
        await whep.aclose()

    app = FastAPI(title="uav-ui", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session = session

    def origin_allowed(origin):
        return origin is None or urlsplit(origin).hostname in settings.allowed_hosts

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # This API can move a drone. Any web page open in the browser can send requests to localhost,
        # so the Host header (DNS rebinding) and the Origin header (cross-site POST) are both checked.
        if _host_name(request.headers.get("host")) not in settings.allowed_hosts:
            return JSONResponse({"error": "host not allowed"}, 403)
        if request.method in UNSAFE_METHODS and not origin_allowed(request.headers.get("origin")):
            return JSONResponse({"error": "origin not allowed"}, 403)
        return await call_next(request)

    # ------------------------------------------------------------------ connection

    @app.get("/api/status")
    async def status():
        return session.status()

    @app.get("/api/profiles")
    async def get_profiles():
        try:
            return session.profiles.load()
        except ProfileError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.put("/api/profiles")
    async def put_profiles(profiles: list = Body(...)):
        try:
            session.profiles.save(profiles)
        except ProfileError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/connect")
    async def connect(body: dict):
        try:
            await session.connect(body.get("name"), body.get("plugin"))
        except ProfileError as exc:
            raise HTTPException(400, str(exc)) from exc
        return session.status()

    @app.post("/api/disconnect")
    async def disconnect():
        await session.disconnect()
        return {"ok": True}

    # ------------------------------------------------------------------ flight

    @app.post("/api/flight/{name}")
    async def flight(name: str, body: dict):
        if body.get("confirmed") is not True:
            raise HTTPException(400, "komenda lotu wymaga potwierdzenia")
        if session.mavlink is None:
            return {"status": "no_vehicle", "error": "brak połączenia MAVLink"}
        result = await session.mavlink.flight_command(name)
        session.add_event(
            "warn" if result["status"] == "accepted" else "error",
            "GUI",
            f"Komenda lotu {name.upper()}: {result['status']}",
        )
        return result

    # ------------------------------------------------------------------ plugins

    def public_plugin(plugin):
        return {
            **{key: value for key, value in plugin.items() if not key.startswith("_")},
            "origin": plugin["_origin"],
            "sha256": plugin["_sha256"],
            "config_values": plugins.config_values(plugin),
        }

    @app.get("/api/plugins")
    async def list_plugins():
        found, problems = plugins.list()
        return {"plugins": [public_plugin(p) for p in found.values()], "problems": problems}

    @app.post("/api/plugins")
    async def upload_plugin(file: UploadFile):
        data = await file.read(MAX_UNPACKED_BYTES + 1)
        if len(data) > MAX_UNPACKED_BYTES:
            raise HTTPException(413, "plik jest za duży")
        try:
            plugin = plugins.install_zip(data)
        except PluginValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": plugin["id"], "name": plugin["name"], "version": plugin.get("version")}

    @app.delete("/api/plugins/{plugin_id}")
    async def delete_plugin(plugin_id: str):
        if not plugins.remove(plugin_id):
            raise HTTPException(404, "nie ma takiego wgranego pluginu")
        return {"ok": True}

    def require_plugin(plugin_id):
        plugin = plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, f"nie ma pluginu '{plugin_id}'")
        return plugin

    @app.put("/api/plugins/{plugin_id}/config")
    async def put_config(plugin_id: str, values: dict):
        plugin = require_plugin(plugin_id)
        try:
            return plugins.save_config(plugin, values)
        except PluginValidationError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/plugin-assets/{plugin_id}/{name}")
    async def plugin_asset(plugin_id: str, name: str):
        path = plugins.asset(plugin_id, name)
        if path is None:
            raise HTTPException(404, "nie ma takiego pliku")
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": CUSTOM_PANEL_CSP,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/plugins/{plugin_id}/commands/{name}")
    async def run_command(plugin_id: str, name: str, body: dict):
        plugin = require_plugin(plugin_id)
        command = (plugin.get("commands") or {}).get(name)
        if command is None:
            raise HTTPException(404, f"nie ma komendy '{name}'")
        if command.get("confirm") and body.get("confirmed") is not True:
            raise HTTPException(400, "ta komenda wymaga potwierdzenia")

        args = dict(body.get("args") or {})
        config = plugins.config_values(plugin)
        for arg, spec in (command.get("args") or {}).items():
            default = spec.get("default")
            if arg not in args and isinstance(default, str) and default.startswith("$config."):
                args[arg] = config.get(default[len("$config.") :])

        if command.get("mavlink"):
            if session.mavlink is None:
                result = {"status": "no_vehicle", "error": "brak połączenia MAVLink"}
            else:
                mavlink = command["mavlink"]
                try:
                    params = [
                        args[p[1:]] if isinstance(p, str) and p.startswith("$") else p
                        for p in mavlink.get("params", [])
                    ]
                except KeyError as exc:
                    raise HTTPException(400, f"brak argumentu {exc}") from exc
                result = await session.mavlink.command_long(mavlink["command"], params)
        elif session.bridge is None:
            result = {"status": "no_bridge", "error": "brak połączenia z bridge'em na dronie"}
        else:
            result = await session.bridge.run_command(name, args)

        level = "info" if result.get("status") in ("ok", "accepted") else "warn"
        session.add_event(level, "GUI", f"{command['label']}: {result.get('status')}")
        return result

    @app.post("/api/actions/{action_id}/cancel")
    async def cancel_action(action_id: str):
        if session.bridge is None:
            raise HTTPException(409, "brak połączenia z bridge'em")
        return await session.bridge.cancel_action(action_id)

    @app.get("/api/plugins/{plugin_id}/images/{name}")
    async def plugin_image(plugin_id: str, name: str, request: Request):
        require_plugin(plugin_id)
        if session.bridge is None:
            return Response(status_code=204)
        try:
            code, content, etag = await session.bridge.get_image(
                name, request.headers.get("if-none-match")
            )
        except httpx.HTTPError:
            return Response(status_code=204)
        headers = {"Cache-Control": "no-store"}
        if etag:
            headers["ETag"] = etag
        if code == 200:
            return Response(content, media_type="image/jpeg", headers=headers)
        return Response(status_code=code if code in (204, 304, 404) else 204, headers=headers)

    # ------------------------------------------------------------------ plans

    @app.get("/api/plans/{scope}")
    async def list_plans(scope: str):
        try:
            return plans.list(scope)
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/plans/{scope}/{name}")
    async def save_plan(scope: str, name: str, plan: dict):
        try:
            plans.save(scope, name, plan)
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/plans/{scope}/{name}")
    async def delete_plan(scope: str, name: str):
        try:
            if not plans.delete(scope, name):
                raise HTTPException(404, "nie ma takiego planu")
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    # ------------------------------------------------------------------ map

    @app.get("/api/map/sources")
    async def map_sources():
        return {"sources": tiles.public_sources(), "global_scope": GLOBAL_SCOPE}

    @app.get("/tiles/{source}/{z}/{x}/{y}")
    async def get_tile(source: str, z: int, x: int, y: int):
        try:
            data = await tiles.get_tile(source, z, x, y)
        except TileError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError:
            return Response(status_code=503)  # offline and not in the cache: the map shows a gap
        if data is None:
            return Response(status_code=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/tiles/prefetch")
    async def prefetch(body: dict):
        try:
            return tiles.start_prefetch(
                body.get("source"),
                float(body["west"]),
                float(body["south"]),
                float(body["east"]),
                float(body["north"]),
                int(body["min_zoom"]),
                int(body["max_zoom"]),
            )
        except (TileError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/tiles/prefetch/{job_id}")
    async def prefetch_status(job_id: int):
        job = tiles.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "nie ma takiego zadania")
        return job

    # ------------------------------------------------------------------ video (WebRTC signalling)

    @app.api_route("/video/whep", methods=["POST"])
    async def whep_offer(request: Request):
        return await _proxy_whep(request, "/video/whep")

    @app.api_route("/video/whep/{rest:path}", methods=["PATCH", "DELETE"])
    async def whep_session(rest: str, request: Request):
        return await _proxy_whep(request, f"/video/whep/{rest}")

    async def _proxy_whep(request, path):
        headers = {}
        if "content-type" in request.headers:
            headers["Content-Type"] = request.headers["content-type"]
        for name in ("if-match",):
            if name in request.headers:
                headers[name] = request.headers[name]
        try:
            response = await whep.request(
                request.method,
                settings.whep_url + path,
                content=await request.body(),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return JSONResponse({"error": f"serwer wideo nie odpowiada: {exc}"}, 502)
        forwarded = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in ("location", "etag", "link", "accept-patch", "content-type")
        }
        return Response(response.content, response.status_code, headers=forwarded)

    # ------------------------------------------------------------------ live data

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        # Browsers do not apply same-origin rules to WebSockets, so the check is ours.
        if not origin_allowed(socket.headers.get("origin")) or (
            _host_name(socket.headers.get("host")) not in settings.allowed_hosts
        ):
            await socket.close(code=1008)
            return
        await socket.accept()
        session.clients.add(socket)
        try:
            await socket.send_json(session.snapshot())
            while True:
                await socket.receive_text()  # nothing is expected, this only detects disconnects
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            session.clients.discard(socket)

    # ------------------------------------------------------------------ frontend

    if settings.static_dir.is_dir():

        class _Spa(StaticFiles):
            async def get_response(self, path, scope):
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404 and not path.startswith(("api/", "tiles/", "video/")):
                        return await super().get_response("index.html", scope)
                    raise

        app.mount("/", _Spa(directory=settings.static_dir, html=True), name="frontend")

    return app
