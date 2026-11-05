"""The active connection: which vehicle, and which plugin. Everything in a profile is optional."""

import asyncio
import collections
import json
import time
from pathlib import Path

import yaml

from .bridge_client import BridgeClient
from .mavlink_link import MavlinkLink
from .udp import VideoTee

EVENT_MEMORY = 500
TELEMETRY_PERIOD_S = 0.25

DEFAULT_PROFILES = [
    {
        "name": "WFB (dron)",
        "mavlink": {"listen": 14570},
        "video": {"listen": 5610},
        "bridge": "http://10.5.0.2:8765",
        "plugin": None,
    },
    {
        "name": "Symulacja",
        # PX4 SITL sends a second MAVLink stream here, so QGC can keep port 14550 at the same time.
        "mavlink": {"listen": 14540},
        "video": {"listen": 5610},
        "bridge": "http://127.0.0.1:8765",
        "plugin": None,
    },
]


class ProfileError(Exception):
    pass


def validate_profile(profile):
    if not isinstance(profile, dict) or not str(profile.get("name", "")).strip():
        raise ProfileError("profil musi mieć nazwę")
    unknown = set(profile) - {"name", "mavlink", "video", "bridge", "plugin"}
    if unknown:
        raise ProfileError(f"nieznane pola profilu: {', '.join(sorted(unknown))}")
    for key in ("mavlink", "video"):
        section = profile.get(key)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ProfileError(f"{key}: oczekiwano listen i forward")
        for field, value in section.items():
            if field not in ("listen", "forward"):
                raise ProfileError(f"{key}.{field}: nieznane pole")
            if value is None and field == "forward":
                continue
            if not isinstance(value, int) or not 1 <= value <= 65535:
                raise ProfileError(f"{key}.{field}: port musi być liczbą 1-65535")
        if "listen" not in section:
            raise ProfileError(f"{key}: brak portu listen")
    bridge = profile.get("bridge")
    if bridge is not None and not str(bridge).startswith(("http://", "https://")):
        raise ProfileError("bridge: adres musi zaczynać się od http://")


class ProfileStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "profiles.yaml"

    def load(self):
        try:
            profiles = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except OSError:
            return [dict(profile) for profile in DEFAULT_PROFILES]
        except yaml.YAMLError as exc:
            raise ProfileError(f"{self.path}: {exc}") from exc
        return profiles if isinstance(profiles, list) else []

    def save(self, profiles):
        if not isinstance(profiles, list):
            raise ProfileError("oczekiwano listy profili")
        names = set()
        for profile in profiles:
            validate_profile(profile)
            if profile["name"] in names:
                raise ProfileError(f"powtórzona nazwa profilu: {profile['name']}")
            names.add(profile["name"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(profiles, allow_unicode=True), encoding="utf-8")


class Session:
    def __init__(self, settings, plugin_store):
        self.settings = settings
        self.plugin_store = plugin_store
        self.profiles = ProfileStore(settings.data_dir)
        self.clients = set()
        self.lock = asyncio.Lock()
        self.profile = None
        self.mavlink = None
        self.video = None
        self.bridge = None
        self._telemetry_task = None
        self.last_telemetry = None
        self._reset_data()

    def _reset_data(self):
        self.state = {}
        self.objects = {}
        self.actions = {}
        self.events = collections.deque(maxlen=EVENT_MEMORY)
        self.event_counter = 0

    async def start(self):
        self._telemetry_task = asyncio.create_task(self._telemetry_loop())

    async def stop(self):
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
        await self.disconnect()

    # ---------------------------------------------------------------- connection

    async def connect(self, name, plugin_id=None):
        profile = next((p for p in self.profiles.load() if p.get("name") == name), None)
        if profile is None:
            raise ProfileError(f"nie ma profilu '{name}'")
        validate_profile(profile)
        if plugin_id:
            profile = {**profile, "plugin": plugin_id}
        await self.disconnect()
        async with self.lock:
            self._reset_data()
            started = []
            try:
                if profile.get("mavlink"):
                    section = profile["mavlink"]
                    self.mavlink = MavlinkLink(section["listen"], section.get("forward"), self.add_event)
                    await self.mavlink.start()
                    started.append(self.mavlink)
                if profile.get("video"):
                    section = profile["video"]
                    self.video = VideoTee(
                        section["listen"], self.settings.video_sink_port, section.get("forward")
                    )
                    await self.video.start()
                    started.append(self.video)
            except OSError as exc:
                for item in started:
                    item.stop()
                self.mavlink = self.video = None
                raise ProfileError(
                    f"nie można otworzyć portu: {exc}. Czy inny program, np. QGC, już go zajmuje?"
                ) from exc
            self.profile = profile
            if profile.get("bridge"):
                self.bridge = BridgeClient(profile["bridge"], self._on_bridge_update)
                await self.bridge.start()
        self.add_event("info", "GUI", f"Połączono: {name}")
        await self.broadcast(self.snapshot())

    async def disconnect(self):
        async with self.lock:
            if self.mavlink is not None:
                self.mavlink.stop()
            if self.video is not None:
                self.video.stop()
            if self.bridge is not None:
                await self.bridge.stop()
            self.mavlink = self.video = self.bridge = self.profile = None
            self.last_telemetry = None
        await self.broadcast(self.snapshot())

    # ---------------------------------------------------------------- data

    def add_event(self, level, source, text):
        self.event_counter += 1
        event = {
            "id": f"g{self.event_counter}",
            "t": time.time(),
            "level": level,
            "source": source,
            "text": text,
        }
        self.events.append(event)
        self._send_soon({"type": "events", "events": [event]})

    def _on_bridge_update(self, update):
        message = {"type": "bridge"}
        if update.get("bridge"):
            message["status"] = self.bridge.status() if self.bridge else None
        if update.get("state"):
            self.state.update(update["state"])
            message["state"] = update["state"]
        if update.get("objects"):
            for item in update["objects"]:
                self.objects[item["id"]] = item
            message["objects"] = update["objects"]
        if update.get("actions"):
            self.actions.update(update["actions"])
            message["actions"] = update["actions"]
        if update.get("events"):
            events = [{**event, "id": f"b{event['seq']}"} for event in update["events"]]
            self.events.extend(events)
            message["events"] = events
        self._send_soon(message)

    def status(self):
        plugin_warning = None
        info = self.bridge.info if self.bridge else None
        plugin_id = self.profile.get("plugin") if self.profile else None
        if info and plugin_id:
            local = self.plugin_store.get(plugin_id)
            remote = info.get("plugin", {})
            if local is None:
                plugin_warning = f"Nie ma wgranego pluginu '{plugin_id}'."
            elif remote.get("id") != plugin_id:
                plugin_warning = f"Na dronie działa inny plugin: {remote.get('id')}."
            elif remote.get("sha256") != local["_sha256"]:
                plugin_warning = (
                    f"Plugin '{plugin_id}' na dronie (wersja {remote.get('version')}) różni się od "
                    f"wgranego tutaj (wersja {local.get('version')}). Wgraj aktualny plik."
                )
        video = None
        if self.video is not None:
            video = {"packets": self.video.packets, "bytes": self.video.bytes}
        return {
            "profile": self.profile,
            "bridge": self.bridge.status() if self.bridge else None,
            "video": video,
            "plugin_warning": plugin_warning,
        }

    def snapshot(self):
        return {
            "type": "snapshot",
            "status": self.status(),
            "telemetry": self.mavlink.snapshot() if self.mavlink else None,
            "state": self.state,
            "objects": list(self.objects.values()),
            "actions": self.actions,
            "events": list(self.events),
        }

    # ---------------------------------------------------------------- browser side

    def _send_soon(self, message):
        try:
            asyncio.get_running_loop().create_task(self.broadcast(message))
        except RuntimeError:
            pass

    async def broadcast(self, message):
        if not self.clients:
            return
        text = json.dumps(message, separators=(",", ":"), default=str)
        for client in list(self.clients):
            try:
                await client.send_text(text)
            except Exception:  # a closed tab must not stop the others
                self.clients.discard(client)

    async def _telemetry_loop(self):
        tick = 0
        while True:
            await asyncio.sleep(TELEMETRY_PERIOD_S)
            tick += 1
            if self.mavlink is not None:
                telemetry = self.mavlink.snapshot()
                if telemetry != self.last_telemetry:
                    self.last_telemetry = telemetry
                    await self.broadcast({"type": "telemetry", "telemetry": telemetry})
            if tick % 4 == 0 and self.profile is not None:
                await self.broadcast({"type": "status", "status": self.status()})
