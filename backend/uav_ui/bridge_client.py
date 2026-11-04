"""Talks to uav_ui_bridge on the drone. One client and one poll loop, however many browser tabs."""

import asyncio
import collections
import time
import uuid

import httpx

POLL_PERIOD_S = 1.0
REQUEST_TIMEOUT_S = 4.0
COMMAND_TIMEOUT_S = 10.0
COMMAND_ATTEMPTS = 2  # same id on both: the bridge does not run a command twice
OFFLINE_AFTER_FAILURES = 3
RATE_WINDOW_S = 10.0


class BridgeClient:
    def __init__(self, url, on_update):
        self.url = url.rstrip("/")
        self.on_update = on_update  # called with a dict holding only what changed
        self.client = None
        self.task = None
        self.online = False
        self.info = None
        self.error = None
        self.seen = {"state": 0, "events": 0, "map": 0, "actions": 0}
        self.failures = 0
        self.transfers = collections.deque()
        self.instance = None

    async def start(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S), headers={"User-Agent": "uav-ui"}, trust_env=False
        )
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.client is not None:
            await self.client.aclose()

    def status(self):
        now = time.monotonic()
        while self.transfers and now - self.transfers[0][0] > RATE_WINDOW_S:
            self.transfers.popleft()
        return {
            "url": self.url,
            "online": self.online,
            "error": self.error,
            "bytes_per_s": round(sum(size for _, size in self.transfers) / RATE_WINDOW_S),
            "info": self.info,
        }

    def _count(self, response):
        self.transfers.append((time.monotonic(), len(response.content) + 200))

    async def _loop(self):
        while True:
            started = time.monotonic()
            try:
                if self.info is None:
                    response = await self.client.get(f"{self.url}/info")
                    response.raise_for_status()
                    self._count(response)
                    self.info = response.json()
                    self.on_update({"bridge": True})
                response = await self.client.get(
                    f"{self.url}/poll",
                    params=self.seen,
                )
                response.raise_for_status()
                self._count(response)
                self._handle_poll(response.json())
                self.failures = 0
                if not self.online or self.error:
                    self.online, self.error = True, None
                    self.on_update({"bridge": True})
            except (httpx.HTTPError, ValueError) as exc:
                self.failures += 1
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                if self.online and self.failures >= OFFLINE_AFTER_FAILURES:
                    self.online = False
                    self.on_update({"bridge": True})
                elif not self.online:
                    self.on_update({"bridge": True})
            await asyncio.sleep(max(0.05, POLL_PERIOD_S - (time.monotonic() - started)))

    def _handle_poll(self, poll):
        seq = int(poll.get("seq", 0))
        if seq < self.seen["state"]:
            # The bridge restarted, so its sequence started over: ask for everything again.
            self.seen = {key: 0 for key in self.seen}
            self.info = None
        else:
            self.seen = {key: seq for key in self.seen}
        update = {
            key: poll[key] for key in ("state", "events", "objects", "actions") if poll.get(key)
        }
        if update:
            self.on_update(update)

    async def run_command(self, name, args):
        command_id = uuid.uuid4().hex
        last_error = "brak odpowiedzi"
        for _attempt in range(COMMAND_ATTEMPTS):
            try:
                response = await self.client.post(
                    f"{self.url}/commands/{name}",
                    json={"id": command_id, "args": args},
                    timeout=COMMAND_TIMEOUT_S,
                )
                self._count(response)
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:200]
        return {
            "id": command_id,
            "status": "no_ack",
            "error": f"brak potwierdzenia: komenda mogła dotrzeć ({last_error})",
        }

    async def cancel_action(self, action_id):
        try:
            response = await self.client.post(f"{self.url}/actions/{action_id}/cancel", json={})
            self._count(response)
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"id": action_id, "status": "no_ack", "error": str(exc)[:200]}

    async def get_image(self, name, etag=None):
        headers = {"If-None-Match": etag} if etag else {}
        response = await self.client.get(f"{self.url}/image/{name}", headers=headers)
        self._count(response)
        return response.status_code, response.content, response.headers.get("ETag")
