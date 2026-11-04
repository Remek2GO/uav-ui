import asyncio
import math
import socket
import time

import pytest
from pymavlink.dialects.v20 import common as mav

from uav_ui.mavlink_link import MavlinkLink, px4_mode_name
from uav_ui.plans import PlanError, PlanStore
from uav_ui.session import ProfileError, ProfileStore, validate_profile
from uav_ui.tiles import MAX_PREFETCH_TILES, TileCache, TileError, count_tiles, tiles_in_bbox
from uav_ui.udp import UdpLink, VideoTee


def feature(geometry):
    return {"type": "Feature", "properties": {}, "geometry": geometry}


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------ plans


def test_plan_round_trip(tmp_path):
    store = PlanStore(tmp_path)
    plan = feature({"type": "LineString", "coordinates": [[19.9, 50.0, 10], [19.91, 50.01, 10]]})
    store.save("hydrolab", "trasa 1", plan)
    assert store.list("hydrolab") == [{"name": "trasa 1", "plan": plan}]
    assert store.list("inny") == []
    assert store.delete("hydrolab", "trasa 1") is True
    assert store.delete("hydrolab", "trasa 1") is False


@pytest.mark.parametrize(
    "plan",
    [
        {},
        feature({"type": "Polygon", "coordinates": []}),
        feature({"type": "Point", "coordinates": [200, 50]}),
        feature({"type": "Point", "coordinates": [19, 91]}),
        feature({"type": "Point", "coordinates": [float("nan"), 50]}),
        feature({"type": "Point", "coordinates": ["a", 50]}),
        feature({"type": "LineString", "coordinates": []}),
        feature({"type": "LineString", "coordinates": [[19, 50]] * 501}),
    ],
)
def test_invalid_plans_are_rejected(tmp_path, plan):
    with pytest.raises(PlanError):
        PlanStore(tmp_path).save("s", "n", plan)


@pytest.mark.parametrize("scope, name", [("../x", "n"), ("s", "../n"), ("s", "a/b"), ("s", ".hidden"), ("S", "n")])
def test_plan_paths_cannot_escape(tmp_path, scope, name):
    with pytest.raises(PlanError):
        PlanStore(tmp_path).save(scope, name, feature({"type": "Point", "coordinates": [19, 50]}))


# ------------------------------------------------------------------ profiles


def test_default_profiles_are_valid(tmp_path):
    store = ProfileStore(tmp_path)
    profiles = store.load()
    store.save(profiles)
    assert [p["name"] for p in store.load()] == [p["name"] for p in profiles]


def test_profile_with_nothing_but_a_name_is_valid():
    validate_profile({"name": "tylko mapa"})


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"name": " "},
        {"name": "a", "unknown": 1},
        {"name": "a", "mavlink": {"forward": 14550}},
        {"name": "a", "mavlink": {"listen": 70000}},
        {"name": "a", "video": {"listen": "x"}},
        {"name": "a", "bridge": "ftp://x"},
    ],
)
def test_invalid_profiles(profile):
    with pytest.raises(ProfileError):
        validate_profile(profile)


def test_duplicate_profile_names_are_rejected(tmp_path):
    with pytest.raises(ProfileError):
        ProfileStore(tmp_path).save([{"name": "a"}, {"name": "a"}])


# ------------------------------------------------------------------ tiles


def test_tile_numbers_follow_the_standard_grid():
    assert list(tiles_in_bbox(-1, 1, -1, 1, 1, 1)) == [(1, 0, 0)]
    assert list(tiles_in_bbox(1, -1, 1, -1, 1, 1)) == [(1, 1, 1)]
    # Kraków, checked against the OpenStreetMap tile numbering
    assert list(tiles_in_bbox(19.9172, 50.0598, 19.9172, 50.0598, 10, 10)) == [(10, 568, 347)]


def test_count_matches_iteration():
    area = (19.90, 50.05, 19.93, 50.07, 12, 15)
    assert count_tiles(*area) == len(list(tiles_in_bbox(*area)))


def test_prefetch_limits(tmp_path):
    cache = TileCache(tmp_path)
    with pytest.raises(TileError, match="nie można pobrać"):
        cache.start_prefetch("esri", 19, 50, 19.1, 50.1, 10, 12)
    with pytest.raises(TileError, match="limit"):
        cache.start_prefetch("orto", 14, 49, 24, 55, 1, 19)
    with pytest.raises(TileError, match="nieprawidłowy obszar"):
        cache.start_prefetch("orto", 19.1, 50, 19, 50.1, 10, 12)
    assert count_tiles(14, 49, 24, 55, 1, 19) > MAX_PREFETCH_TILES


def test_esri_goes_directly_from_the_browser(tmp_path):
    sources = {s["id"]: s for s in TileCache(tmp_path).public_sources()}
    assert sources["esri"]["tiles"].startswith("https://")
    assert sources["orto"]["tiles"] == "/tiles/orto/{z}/{x}/{y}"


def test_esri_tile_is_not_cached_by_the_backend(tmp_path):
    with pytest.raises(TileError):
        asyncio.run(TileCache(tmp_path).get_tile("esri", 10, 1, 1))


def test_cached_tile_is_served_without_network(tmp_path):
    cache = TileCache(tmp_path)  # no client: any network use would raise
    path = cache._path("orto", 10, 5, 6)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"jpeg")
    assert asyncio.run(cache.get_tile("orto", 10, 5, 6)) == b"jpeg"


# ------------------------------------------------------------------ UDP


def test_udp_link_relays_both_ways():
    async def scenario():
        listen, qgc_port = free_port(), free_port()
        received = []
        link = UdpLink(listen, qgc_port, received.append)
        await link.start()
        loop = asyncio.get_running_loop()

        qgc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        qgc.bind(("127.0.0.1", qgc_port))
        qgc.setblocking(False)
        wfb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        wfb.bind(("127.0.0.1", 0))
        wfb.setblocking(False)
        try:
            wfb.sendto(b"down", ("127.0.0.1", listen))
            data, relay = await asyncio.wait_for(loop.sock_recvfrom(qgc, 2048), 2)
            assert data == b"down" and received == [b"down"]

            qgc.sendto(b"up", relay)
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(wfb, 2048), 2)
            assert data == b"up"
        finally:
            link.stop()
            qgc.close()
            wfb.close()

    asyncio.run(scenario())


def test_video_tee_copies_to_every_target():
    async def scenario():
        listen, sink_port, qgc_port = free_port(), free_port(), free_port()
        tee = VideoTee(listen, sink_port, qgc_port)
        await tee.start()
        loop = asyncio.get_running_loop()
        sockets = []
        for port in (sink_port, qgc_port):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", port))
            sock.setblocking(False)
            sockets.append(sock)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"rtp", ("127.0.0.1", listen))
            for sock in sockets:
                data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), 2)
                assert data == b"rtp"
            assert tee.packets == 1
        finally:
            tee.stop()
            sender.close()
            for sock in sockets:
                sock.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ MAVLink


class FakeAutopilot:
    """Talks to MavlinkLink over a real UDP socket, like PX4 does."""

    def __init__(self, port, autopilot=mav.MAV_AUTOPILOT_PX4, armed=True, custom_mode=6 << 16):
        self.port = port
        self.mav = mav.MAVLink(None, srcSystem=1, srcComponent=mav.MAV_COMP_ID_AUTOPILOT1)
        self.autopilot = autopilot
        self.armed = armed
        self.custom_mode = custom_mode
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)

    def send(self, message):
        self.sock.sendto(message.pack(self.mav), ("127.0.0.1", self.port))

    def heartbeat(self):
        flags = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if self.armed:
            flags |= mav.MAV_MODE_FLAG_SAFETY_ARMED
        self.send(
            mav.MAVLink_heartbeat_message(
                mav.MAV_TYPE_QUADROTOR, self.autopilot, flags, self.custom_mode, mav.MAV_STATE_ACTIVE, 3
            )
        )

    async def next_command(self, timeout=3.0):
        parser = mav.MAVLink(None)
        parser.robust_parsing = True
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + timeout
        while True:
            data, _ = await asyncio.wait_for(
                loop.sock_recvfrom(self.sock, 2048), deadline - time.monotonic()
            )
            for message in parser.parse_buffer(data) or []:
                if message.get_type() == "COMMAND_LONG":
                    return message

    def close(self):
        self.sock.close()


async def _with_link(scenario, **autopilot_args):
    port = free_port()
    events = []
    link = MavlinkLink(port, on_event=lambda *event: events.append(event))
    await link.start()
    autopilot = FakeAutopilot(port, **autopilot_args)
    try:
        autopilot.heartbeat()
        await asyncio.sleep(0.05)
        await scenario(link, autopilot, events)
    finally:
        link.stop()
        autopilot.close()


def test_mode_names():
    assert px4_mode_name(6 << 16) == "OFFBOARD"
    assert px4_mode_name((4 << 16) | (5 << 24)) == "RTL"
    assert px4_mode_name((4 << 16) | (3 << 24)) == "HOLD"
    assert px4_mode_name((4 << 16) | (6 << 24)) == "LAND"


def test_telemetry_is_decoded():
    async def scenario(link, autopilot, _events):
        autopilot.send(
            mav.MAVLink_global_position_int_message(0, 500598000, 199172000, 215000, 15000, 0, 0, 0, 9000)
        )
        autopilot.send(mav.MAVLink_gps_raw_int_message(0, 3, 0, 0, 0, 0, 0, 0, 0, 12))
        await asyncio.sleep(0.05)
        telemetry = link.snapshot()
        assert telemetry["online"] and telemetry["armed"] and telemetry["mode"] == "OFFBOARD"
        assert telemetry["lat"] == pytest.approx(50.0598)
        assert telemetry["rel_alt"] == pytest.approx(15.0)
        assert telemetry["heading"] == pytest.approx(90.0)
        assert telemetry["satellites"] == 12

    asyncio.run(_with_link(scenario))


def test_gcs_heartbeat_does_not_count_as_a_vehicle():
    async def scenario():
        port = free_port()
        link = MavlinkLink(port)
        await link.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        qgc = mav.MAVLink(None, srcSystem=255, srcComponent=190)
        message = mav.MAVLink_heartbeat_message(
            mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE, 3
        )
        sender.sendto(message.pack(qgc), ("127.0.0.1", port))
        await asyncio.sleep(0.05)
        assert link.snapshot()["online"] is False and link.target is None
        link.stop()
        sender.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name, command, param2, param3",
    [
        ("rtl", mav.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0, 0),
        ("land", mav.MAV_CMD_NAV_LAND, 0, 0),
        ("hold", mav.MAV_CMD_DO_SET_MODE, 4, 3),
    ],
)
def test_flight_command_gets_accepted(name, command, param2, param3):
    async def scenario(link, autopilot, _events):
        task = asyncio.create_task(link.flight_command(name))
        received = await autopilot.next_command()
        assert received.command == command and received.target_system == 1
        assert received.param2 == param2 and received.param3 == param3
        autopilot.send(mav.MAVLink_command_ack_message(command, mav.MAV_RESULT_ACCEPTED))
        assert (await task)["status"] == "accepted"

    asyncio.run(_with_link(scenario))


def test_land_keeps_position_unset():
    params = __import__("uav_ui.mavlink_link", fromlist=["FLIGHT_COMMANDS"]).FLIGHT_COMMANDS["land"][1]
    assert math.isnan(params[4]) and math.isnan(params[5])


def test_retries_raise_confirmation_then_report_no_ack():
    async def scenario(link, autopilot, _events):
        task = asyncio.create_task(link.flight_command("rtl"))
        confirmations = [(await autopilot.next_command()).confirmation for _ in range(3)]
        assert confirmations == [0, 1, 2]
        assert (await task)["status"] == "no_ack"

    asyncio.run(_with_link(scenario))


def test_denied_is_reported():
    async def scenario(link, autopilot, _events):
        task = asyncio.create_task(link.flight_command("land"))
        received = await autopilot.next_command()
        autopilot.send(mav.MAVLink_command_ack_message(received.command, mav.MAV_RESULT_DENIED))
        assert (await task)["status"] == "denied"

    asyncio.run(_with_link(scenario))


def test_flight_command_needs_armed_px4():
    async def not_armed(link, _autopilot, _events):
        result = await link.flight_command("rtl")
        assert result["status"] == "rejected" and "uzbrojony" in result["error"]

    async def other_autopilot(link, _autopilot, _events):
        result = await link.flight_command("rtl")
        assert result["status"] == "rejected" and "PX4" in result["error"]

    asyncio.run(_with_link(not_armed, armed=False))
    asyncio.run(_with_link(other_autopilot, autopilot=mav.MAV_AUTOPILOT_ARDUPILOTMEGA))


def test_no_vehicle_is_reported():
    async def scenario():
        link = MavlinkLink(free_port())
        await link.start()
        assert (await link.flight_command("rtl"))["status"] == "no_vehicle"
        link.stop()

    asyncio.run(scenario())


def test_long_statustext_is_put_back_together():
    async def scenario(link, autopilot, events):
        first = b"Arming denied: Resolve system health failures firs"
        assert len(first) == 50
        autopilot.send(mav.MAVLink_statustext_message(4, first, 7, 0))
        autopilot.send(mav.MAVLink_statustext_message(4, b"t", 7, 1))
        await asyncio.sleep(0.05)
        assert events.count(("warn", "PX4", "Arming denied: Resolve system health failures first")) == 1
        assert not any(event[2] == "t" for event in events)

    asyncio.run(_with_link(scenario))


def test_long_statustext_without_chunk_id_from_mavlink1():
    async def scenario(link, autopilot, events):
        first = b"Arming denied: Resolve system health failures firs"
        autopilot.send(mav.MAVLink_statustext_message(4, first))
        autopilot.send(mav.MAVLink_statustext_message(4, b"t"))
        await asyncio.sleep(0.05)
        assert ("warn", "PX4", "Arming denied: Resolve system health failures first") in events
        assert not any(event[2] in ("t", first.decode()) for event in events)

    asyncio.run(_with_link(scenario))


def test_text_of_exactly_full_length_is_not_lost():
    async def scenario(link, autopilot, events):
        text = "x" * 50
        autopilot.send(mav.MAVLink_statustext_message(4, text.encode()))
        await asyncio.sleep(0.03)
        autopilot.heartbeat()
        await asyncio.sleep(0.03)
        assert ("warn", "PX4", text) in events

    asyncio.run(_with_link(scenario))


def test_repeated_status_is_shown_once():
    async def scenario(link, autopilot, events):
        for _ in range(5):
            autopilot.send(mav.MAVLink_statustext_message(4, b"Same text"))
        autopilot.send(mav.MAVLink_statustext_message(4, b"Other text"))
        await asyncio.sleep(0.05)
        assert events.count(("warn", "PX4", "Same text")) == 1
        assert ("warn", "PX4", "Other text") in events

    asyncio.run(_with_link(scenario))


def test_mode_change_and_statustext_become_events():
    async def scenario(link, autopilot, events):
        autopilot.custom_mode = (4 << 16) | (5 << 24)
        autopilot.heartbeat()
        autopilot.send(mav.MAVLink_statustext_message(4, b"Low battery"))
        await asyncio.sleep(0.05)
        assert ("info", "PX4", "Tryb: OFFBOARD → RTL") in events
        assert ("warn", "PX4", "Low battery") in events

    asyncio.run(_with_link(scenario))
