"""MAVLink: vehicle telemetry for the map, plus RTL, Land and Hold.

The GUI never arms, takes off, starts a mission or switches back to Offboard. Those stay with the RC
and QGC.
"""

import asyncio
import math
import time

from pymavlink.dialects.v20 import common as mav

from .udp import UdpLink

GCS_SYSTEM_ID = 250  # QGC is 255, a separate id keeps the two distinguishable in logs
GCS_COMPONENT_ID = mav.MAV_COMP_ID_MISSIONPLANNER
HEARTBEAT_PERIOD_S = 1.0
ONLINE_TIMEOUT_S = 3.0
ACK_TIMEOUT_S = 1.2
COMMAND_ATTEMPTS = 3
STATUSTEXT_CHUNK = 50
STATUS_REPEAT_S = 10.0

PX4_MAIN_MODES = {
    1: "MANUAL",
    2: "ALTCTL",
    3: "POSCTL",
    5: "ACRO",
    6: "OFFBOARD",
    7: "STABILIZED",
    8: "RATTITUDE",
}
PX4_AUTO_MODES = {
    1: "AUTO READY",
    2: "TAKEOFF",
    3: "HOLD",
    4: "MISSION",
    5: "RTL",
    6: "LAND",
    8: "FOLLOW",
    9: "PRECLAND",
}
PX4_MAIN_MODE_AUTO = 4
PX4_AUTO_LOITER = 3

ACK_RESULTS = {
    mav.MAV_RESULT_ACCEPTED: "accepted",
    mav.MAV_RESULT_TEMPORARILY_REJECTED: "temporarily_rejected",
    mav.MAV_RESULT_DENIED: "denied",
    mav.MAV_RESULT_UNSUPPORTED: "unsupported",
    mav.MAV_RESULT_FAILED: "failed",
    mav.MAV_RESULT_IN_PROGRESS: "in_progress",
}

FLIGHT_COMMANDS = {
    "rtl": (mav.MAV_CMD_NAV_RETURN_TO_LAUNCH, [0, 0, 0, 0, 0, 0, 0]),
    "land": (mav.MAV_CMD_NAV_LAND, [0, 0, 0, math.nan, math.nan, math.nan, math.nan]),
    "hold": (
        mav.MAV_CMD_DO_SET_MODE,
        [mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, PX4_MAIN_MODE_AUTO, PX4_AUTO_LOITER, 0, 0, 0, 0],
    ),
}

STATUS_LEVELS = {0: "fatal", 1: "fatal", 2: "fatal", 3: "error", 4: "warn", 5: "info", 6: "info", 7: "debug"}


def px4_mode_name(custom_mode):
    main = (custom_mode >> 16) & 0xFF
    sub = (custom_mode >> 24) & 0xFF
    if main == PX4_MAIN_MODE_AUTO:
        return PX4_AUTO_MODES.get(sub, f"AUTO {sub}")
    return PX4_MAIN_MODES.get(main, f"MODE {main}")


class MavlinkLink:
    def __init__(self, listen_port, forward_port=None, on_event=None):
        self.link = UdpLink(listen_port, forward_port, self._on_datagram)
        self.on_event = on_event or (lambda *_: None)
        self.parser = mav.MAVLink(None, srcSystem=GCS_SYSTEM_ID, srcComponent=GCS_COMPONENT_ID)
        self.parser.robust_parsing = True
        self.target = None
        self.last_heartbeat = 0.0
        self.pending_acks = {}
        self.status_chunks = {}
        self.unnumbered_chunk = ""
        self.unnumbered_level = "info"
        self.recent_status = {}
        self.telemetry = {
            "online": False,
            "autopilot": None,
            "armed": False,
            "mode": None,
            "lat": None,
            "lon": None,
            "alt": None,
            "rel_alt": None,
            "heading": None,
            "groundspeed": None,
            "climb": None,
            "battery_pct": None,
            "battery_v": None,
            "gps_fix": None,
            "satellites": None,
            "home": None,
            "rssi": None,
        }
        self._heartbeat_task = None

    async def start(self):
        await self.link.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def stop(self):
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        self.link.stop()

    def snapshot(self):
        self.telemetry["online"] = time.monotonic() - self.last_heartbeat < ONLINE_TIMEOUT_S
        return dict(self.telemetry)

    # ---------------------------------------------------------------- receiving

    def _on_datagram(self, data):
        try:
            messages = self.parser.parse_buffer(data) or []
        except mav.MAVError:
            return
        for message in messages:
            handler = getattr(self, f"_on_{message.get_type().lower()}", None)
            if handler is not None:
                handler(message)

    def _on_heartbeat(self, message):
        # The stream also carries heartbeats of other GCSes and of wfb-ng itself.
        if message.autopilot == mav.MAV_AUTOPILOT_INVALID or message.type == mav.MAV_TYPE_GCS:
            return
        if message.get_srcComponent() != mav.MAV_COMP_ID_AUTOPILOT1:
            return
        self.target = (message.get_srcSystem(), message.get_srcComponent())
        self.last_heartbeat = time.monotonic()
        if self.unnumbered_chunk:
            self._emit_status(self.unnumbered_level, self.unnumbered_chunk)
            self.unnumbered_chunk = ""
        self.telemetry["armed"] = bool(message.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)
        if message.autopilot == mav.MAV_AUTOPILOT_PX4:
            self.telemetry["autopilot"] = "px4"
            mode = px4_mode_name(message.custom_mode)
        else:
            self.telemetry["autopilot"] = "other"
            mode = f"MODE {message.custom_mode}"
        previous = self.telemetry["mode"]
        self.telemetry["mode"] = mode
        if previous is not None and previous != mode:
            self.on_event("info", "PX4", f"Tryb: {previous} → {mode}")

    def _on_global_position_int(self, message):
        if message.lat == 0 and message.lon == 0:
            return  # no position yet
        self.telemetry.update(
            lat=message.lat / 1e7,
            lon=message.lon / 1e7,
            alt=message.alt / 1000.0,
            rel_alt=message.relative_alt / 1000.0,
            heading=None if message.hdg == 65535 else message.hdg / 100.0,
        )

    def _on_vfr_hud(self, message):
        self.telemetry.update(groundspeed=round(message.groundspeed, 2), climb=round(message.climb, 2))

    def _on_sys_status(self, message):
        self.telemetry["battery_pct"] = None if message.battery_remaining < 0 else message.battery_remaining
        self.telemetry["battery_v"] = (
            None if message.voltage_battery == 65535 else round(message.voltage_battery / 1000.0, 2)
        )

    def _on_gps_raw_int(self, message):
        self.telemetry.update(gps_fix=message.fix_type, satellites=message.satellites_visible)

    def _on_home_position(self, message):
        self.telemetry["home"] = {"lat": message.latitude / 1e7, "lon": message.longitude / 1e7}

    def _on_radio_status(self, message):
        self.telemetry["rssi"] = message.rssi

    def _on_statustext(self, message):
        text = message.text.rstrip("\x00") if isinstance(message.text, str) else ""
        level = STATUS_LEVELS.get(message.severity, "info")
        if message.id == 0:
            # PX4 starts a link in MAVLink 1, which has no chunk id. A full-length text is taken as
            # the start of a longer one. A heartbeat flushes it, should it really end there.
            text = self.unnumbered_chunk + text
            self.unnumbered_chunk = ""
            if len(text) % STATUSTEXT_CHUNK == 0 and text:
                self.unnumbered_chunk = text
                self.unnumbered_level = level
            else:
                self._emit_status(level, text)
            return
        # Longer texts come in chunks of 50 characters with a shared id. A shorter chunk is the last.
        if len(self.status_chunks) > 8:
            self.status_chunks.clear()
        self.status_chunks[message.id] = self.status_chunks.get(message.id, "") + text
        if len(text) < STATUSTEXT_CHUNK:
            self._emit_status(level, self.status_chunks.pop(message.id))

    def _emit_status(self, level, text):
        text = text.strip()
        if not text:
            return
        # A mission retrying to arm makes PX4 repeat the same refusal ten times a second.
        now = time.monotonic()
        if now - self.recent_status.get(text, -STATUS_REPEAT_S) < STATUS_REPEAT_S:
            return
        if len(self.recent_status) > 256:
            self.recent_status.clear()
        self.recent_status[text] = now
        self.on_event(level, "PX4", text)

    def _on_command_ack(self, message):
        future = self.pending_acks.get(message.command)
        if future is not None and not future.done():
            future.set_result(message.result)

    # ---------------------------------------------------------------- sending

    async def _heartbeat_loop(self):
        while True:
            message = mav.MAVLink_heartbeat_message(
                mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE, 3
            )
            self.link.send(message.pack(self.parser))
            await asyncio.sleep(HEARTBEAT_PERIOD_S)

    async def command_long(self, command, params):
        """Sends a command and waits for the ACK. Repeats with a raised confirmation counter, as the
        protocol asks, so a duplicate is not acted on twice by the autopilot."""
        if self.target is None or not self.snapshot()["online"]:
            return {"status": "no_vehicle", "error": "brak heartbeatu z autopilota"}
        if command in self.pending_acks:
            return {"status": "busy", "error": "poprzednia komenda czeka na potwierdzenie"}

        future = asyncio.get_running_loop().create_future()
        self.pending_acks[command] = future
        padded = (list(params) + [0.0] * 7)[:7]
        try:
            for confirmation in range(COMMAND_ATTEMPTS):
                message = mav.MAVLink_command_long_message(
                    self.target[0], self.target[1], command, confirmation, *padded
                )
                self.link.send(message.pack(self.parser))
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), ACK_TIMEOUT_S)
                except asyncio.TimeoutError:
                    continue
                status = ACK_RESULTS.get(result, f"result_{result}")
                return {"status": status, "attempts": confirmation + 1}
            return {
                "status": "no_ack",
                "error": "brak potwierdzenia: komenda mogła dotrzeć, sprawdź tryb drona",
                "attempts": COMMAND_ATTEMPTS,
            }
        finally:
            self.pending_acks.pop(command, None)
            if not future.done():
                future.cancel()

    async def flight_command(self, name):
        if name not in FLIGHT_COMMANDS:
            return {"status": "rejected", "error": f"nieznana komenda {name}"}
        telemetry = self.snapshot()
        if not telemetry["online"]:
            return {"status": "no_vehicle", "error": "brak heartbeatu z autopilota"}
        if telemetry["autopilot"] != "px4":
            return {"status": "rejected", "error": "komendy lotu obsługują w tej wersji tylko PX4"}
        if not telemetry["armed"]:
            return {"status": "rejected", "error": "dron nie jest uzbrojony"}
        command, params = FLIGHT_COMMANDS[name]
        return await self.command_long(command, params)
