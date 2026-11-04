#!/usr/bin/env python3
"""uav_ui_bridge: exposes what a plugin file declares, and nothing else, over HTTP."""

import collections
import re
import threading
import time

import rclpy
import yaml
from rcl_interfaces.msg import Log
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rosidl_runtime_py import set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from . import geo, http_api
from .plugin import (
    ArgError,
    PluginError,
    get_path,
    load_plugin,
    parse_vars,
    render_fields,
    resolve_file,
    to_json_value,
    validate_args,
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "fatal": 50}
LEVEL_NAMES = {10: "debug", 20: "info", 30: "warn", 40: "error", 50: "fatal"}

COMMAND_MEMORY = 128
EVENT_MEMORY = 300
EVENTS_PER_POLL = 100
REPEATED_EVENT_WINDOW_S = 5.0
SERVICE_TIMEOUT_S = 8.0
MATCH_WAIT_S = 1.0
ONESHOT_LINGER_S = 3.0
IMAGE_IDLE_S = 10.0


def _interface_type(name, kind):
    """`std_msgs/String` -> `std_msgs/msg/String`; three-part names pass through."""
    parts = name.split("/")
    return "/".join([parts[0], kind, parts[1]]) if len(parts) == 2 else name


def _qos(spec, default_reliability, default_depth):
    spec = spec or {}
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=int(spec.get("depth", default_depth)),
        reliability=(
            ReliabilityPolicy.BEST_EFFORT
            if spec.get("reliability", default_reliability) == "best_effort"
            else ReliabilityPolicy.RELIABLE
        ),
        durability=(
            DurabilityPolicy.TRANSIENT_LOCAL
            if spec.get("durability") == "transient_local"
            else DurabilityPolicy.VOLATILE
        ),
    )


def _subscriber_qos(spec):
    # Best effort matches every publisher and only the newest sample matters here.
    return _qos(spec, "best_effort", 5)


class BridgeNode(Node):
    def __init__(self):
        super().__init__("uav_ui_bridge")
        self.declare_parameter("plugin_file", "")
        self.declare_parameter("bind_address", "127.0.0.1")
        self.declare_parameter("port", 8765)
        self.declare_parameter("plugin_vars", "")

        self.plugin = load_plugin(
            self.get_parameter("plugin_file").value,
            parse_vars(self.get_parameter("plugin_vars").value),
        )
        self.group = ReentrantCallbackGroup()
        self.lock = threading.RLock()

        self.seq = 0  # one counter: "since" works the same for every kind of data
        self.state = {}
        self.events = collections.deque(maxlen=EVENT_MEMORY)
        self.map_objects = {}
        self.actions = {}
        self.recent_event_times = {}
        self.event_times = collections.deque()

        self.command_results = collections.OrderedDict()
        self.publishers_by_command = {}
        self.clients_by_command = {}
        self.unavailable = {"commands": {}, "state": {}, "layers": {}, "images": {}}
        self.subscriptions_keep = []
        self.topic_handlers = collections.defaultdict(list)
        self.topic_specs = {}

        self.vehicle_position = None
        self.origin = None
        self.images = {}

        self._set_up_commands()
        self._set_up_state()
        self._set_up_events()
        self._set_up_map()
        self._set_up_images()
        self._create_topic_subscriptions()

        self.image_timer = self.create_timer(2.0, self._drop_idle_images, callback_group=self.group)
        self.get_logger().info(
            f"Plugin {self.plugin['id']} {self.plugin.get('version', '?')}: "
            f"{len(self.plugin.get('commands') or {})} commands, "
            f"{sum(len(v) for v in self.unavailable.values())} unavailable items."
        )

    # ------------------------------------------------------------------ set-up

    def _unavailable(self, kind, name, exc):
        self.unavailable[kind][name] = str(exc)
        self.get_logger().warn(f"{kind} '{name}' is unavailable: {exc}")

    def _set_up_commands(self):
        for name, command in (self.plugin.get("commands") or {}).items():
            ros = command.get("ros")
            if not ros:
                continue  # MAVLink commands are the backend's business
            try:
                if "topic" in ros:
                    msg_type = get_message(_interface_type(ros["type"], "msg"))
                    qos = _qos(ros.get("qos"), "reliable", 10)
                    entry = {"kind": "topic", "type": msg_type, "qos": qos, "ros": ros}
                    if not ros.get("oneshot"):
                        entry["publisher"] = self.create_publisher(msg_type, ros["topic"], qos)
                    self.publishers_by_command[name] = entry
                elif "service" in ros:
                    srv_type = get_service(_interface_type(ros["type"], "srv"))
                    client = self.create_client(
                        srv_type, ros["service"], callback_group=self.group
                    )
                    self.clients_by_command[name] = {
                        "kind": "service",
                        "type": srv_type,
                        "client": client,
                        "ros": ros,
                    }
                elif "action" in ros:
                    action_type = get_action(_interface_type(ros["type"], "action"))
                    client = ActionClient(
                        self, action_type, ros["action"], callback_group=self.group
                    )
                    self.clients_by_command[name] = {
                        "kind": "action",
                        "type": action_type,
                        "client": client,
                        "ros": ros,
                    }
                else:
                    raise PluginError("ros needs one of: topic, service, action")
            except (ImportError, AttributeError, ValueError, ModuleNotFoundError) as exc:
                self._unavailable("commands", name, exc)
            except PluginError as exc:
                self._unavailable("commands", name, exc)

    def _subscribe(self, spec, handler):
        """Registers a handler. Several plugin entries on one topic share one subscription."""
        topic = spec["topic"]
        known = self.topic_specs.get(topic)
        if known and known["type"] != spec["type"]:
            raise PluginError(f"topic {topic} declared with two types")
        msg_type = get_message(_interface_type(spec["type"], "msg"))
        if known is None:
            self.topic_specs[topic] = {"type": spec["type"], "cls": msg_type, "qos": spec.get("qos")}
        self.topic_handlers[topic].append(handler)

    def _create_topic_subscriptions(self):
        for topic, spec in self.topic_specs.items():
            handlers = self.topic_handlers[topic]

            def dispatch(msg, handlers=handlers, topic=topic):
                for handler in handlers:
                    try:
                        handler(msg)
                    except Exception as exc:  # one bad field must not stop the other handlers
                        self.get_logger().warn(
                            f"Handler on {topic} failed: {exc}", throttle_duration_sec=10.0
                        )

            self.subscriptions_keep.append(
                self.create_subscription(
                    spec["cls"],
                    topic,
                    dispatch,
                    _subscriber_qos(spec["qos"]),
                    callback_group=self.group,
                )
            )

    def _set_up_state(self):
        for key, spec in (self.plugin.get("state") or {}).items():

            def on_message(msg, key=key, spec=spec):
                value = to_json_value(
                    get_path(msg, spec.get("field", "data")), spec.get("precision", 4)
                )
                self._store_state(key, value)

            try:
                self._subscribe(spec, on_message)
            except (ImportError, AttributeError, ValueError, ModuleNotFoundError, PluginError) as exc:
                self._unavailable("state", key, exc)

    def _store_state(self, key, value):
        with self.lock:
            current = self.state.get(key)
            if current is not None and current["v"] == value:
                current["t"] = time.time()
                return  # not resent: unchanged values cost nothing on the radio
            self.seq += 1
            self.state[key] = {"v": value, "t": time.time(), "seq": self.seq}

    def _set_up_events(self):
        spec = self.plugin.get("events") or {}
        rosout = spec.get("rosout")
        if rosout:
            self.rosout_min = LOG_LEVELS.get(str(rosout.get("min_level", "info")), 20)
            self.rosout_nodes = set(rosout.get("nodes") or [])
            self.rosout_exclude = set(rosout.get("exclude_nodes") or []) | {self.get_name()}
            self.max_events_per_s = float(rosout.get("max_per_s", 5))
            qos = QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=50,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,  # no replay of what happened before start
            )
            self.subscriptions_keep.append(
                self.create_subscription(
                    Log, "/rosout", self._on_rosout, qos, callback_group=self.group
                )
            )
        else:
            self.max_events_per_s = 5.0

        for index, entry in enumerate(spec.get("topics") or []):

            def on_message(msg, entry=entry):
                fields = {}
                for field in re.findall(r"{([^{}]+)}", entry["text"]):
                    fields[field] = get_path(msg, field)
                text = entry["text"]
                for field, value in fields.items():
                    text = text.replace("{" + field + "}", str(to_json_value(value, 2)))
                self._add_event(
                    entry.get("level", "info"),
                    entry.get("source", "plugin"),
                    text,
                    float(entry.get("min_interval_s", REPEATED_EVENT_WINDOW_S)),
                )

            try:
                self._subscribe(entry, on_message)
            except (ImportError, AttributeError, ValueError, ModuleNotFoundError, PluginError) as exc:
                self._unavailable("state", f"event {index}", exc)

    def _on_rosout(self, msg):
        if msg.level < self.rosout_min or msg.name in self.rosout_exclude:
            return
        if self.rosout_nodes and msg.name not in self.rosout_nodes:
            return
        text = ANSI_ESCAPE.sub("", msg.msg).strip()
        if text:
            self._add_event(LEVEL_NAMES.get(int(msg.level), "info"), msg.name, text[:500])

    def _add_event(self, level, source, text, repeat_window_s=REPEATED_EVENT_WINDOW_S):
        now = time.time()
        with self.lock:
            key = (source, text)
            if now - self.recent_event_times.get(key, 0.0) < repeat_window_s:
                return
            while self.event_times and now - self.event_times[0] > 1.0:
                self.event_times.popleft()
            if len(self.event_times) >= self.max_events_per_s and level not in ("error", "fatal"):
                return
            self.event_times.append(now)
            self.recent_event_times[key] = now
            if len(self.recent_event_times) > 512:
                # Longest window a plugin is likely to use: forgetting sooner would only repeat a message.
                self.recent_event_times = {
                    k: t for k, t in self.recent_event_times.items() if now - t < 600.0
                }
            self.seq += 1
            self.events.append(
                {"seq": self.seq, "t": now, "level": level, "source": source, "text": text}
            )

    def _set_up_map(self):
        spec = self.plugin.get("map") or {}
        for name, attr in (("vehicle", "vehicle_position"), ("origin", "origin")):
            source = spec.get(name)
            if source:

                def on_position(msg, source=source, attr=attr):
                    lat = float(get_path(msg, source["lat"]))
                    lon = float(get_path(msg, source["lon"]))
                    alt = float(get_path(msg, source["alt"])) if "alt" in source else 0.0
                    if geo.valid_coordinates(lat, lon):
                        setattr(self, attr, (lat, lon, alt))

                try:
                    self._subscribe(source, on_position)
                except (ImportError, AttributeError, ValueError, ModuleNotFoundError, PluginError) as exc:
                    self._unavailable("layers", name, exc)

        for layer in spec.get("layers") or []:
            try:
                self._set_up_layer(layer)
            except (
                ImportError,
                AttributeError,
                ValueError,
                ModuleNotFoundError,
                PluginError,
                KeyError,
                OSError,
                yaml.YAMLError,
            ) as exc:
                self._unavailable("layers", layer.get("id", "?"), exc)

    def _set_up_layer(self, layer):
        layer_id = layer["id"]
        source = layer["source"]

        if "file" in source:
            path = resolve_file(source["file"], self.plugin["_vars"])
            with open(path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            for marker in geo.markers_from_file(data, source):
                self._store_object(
                    layer_id, marker["key"], {**marker, "kind": "marker"}, min_interval_s=0.0
                )
            return

        at_vehicle = source.get("at") == "vehicle"
        if not at_vehicle and "lat" not in source and geo.is_local_type(source["type"]):
            if not (self.plugin.get("map") or {}).get("origin"):
                raise PluginError("a local-frame layer needs map.origin in the plugin")
        min_interval = float(source.get("min_interval_s", 1.0))

        def on_message(msg):
            key = str(get_path(msg, source["key"])) if "key" in source else layer_id
            common = {"key": key}
            if "label" in source:
                common["label"] = str(get_path(msg, source["label"]))
            if "color" in source:
                common["color"] = str(get_path(msg, source["color"]))

            if at_vehicle:
                if self.vehicle_position is None:
                    return
                points, is_path = [self.vehicle_position], False
            else:
                points, is_path = geo.extract_points(msg, source, self.origin)
            if not points:
                return

            if is_path:
                obj = {
                    **common,
                    "kind": "path",
                    "points": [[round(p[0], 7), round(p[1], 7), round(p[2], 2)] for p in points],
                }
            else:
                lat, lon, alt = points[0]
                obj = {
                    **common,
                    "kind": "marker",
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "alt": round(alt, 2),
                }
            if at_vehicle:
                obj["approximate"] = True
            self._store_object(layer_id, key, obj, min_interval)

        self._subscribe(source, on_message)

    def _store_object(self, layer_id, key, obj, min_interval_s):
        now = time.time()
        object_id = f"{layer_id}/{key}"
        with self.lock:
            current = self.map_objects.get(object_id)
            if current and now - current["t"] < min_interval_s:
                return
            self.seq += 1
            self.map_objects[object_id] = {
                **obj,
                "layer": layer_id,
                "id": object_id,
                "t": now,
                "seq": self.seq,
            }

    def _set_up_images(self):
        for name, spec in (self.plugin.get("images") or {}).items():
            try:
                self.images[name] = {
                    "spec": spec,
                    "cls": get_message(_interface_type(spec["type"], "msg")),
                    "sub": None,
                    "msg": None,
                    "msg_seq": 0,
                    "jpeg": None,
                    "jpeg_seq": 0,
                    "jpeg_time": 0.0,
                    "requested": 0.0,
                }
            except (ImportError, AttributeError, ValueError, ModuleNotFoundError) as exc:
                self._unavailable("images", name, exc)

    # ------------------------------------------------------------------ HTTP API

    def info(self):
        commands = {}
        for name, command in (self.plugin.get("commands") or {}).items():
            if not command.get("ros"):
                continue
            error = self.unavailable["commands"].get(name)
            commands[name] = {"available": error is None}
            if error:
                commands[name]["error"] = error
        return {
            "api": self.plugin["api"],
            "plugin": {
                "id": self.plugin["id"],
                "version": self.plugin.get("version"),
                "sha256": self.plugin["_sha256"],
            },
            "commands": commands,
            "unavailable": self.unavailable,
        }

    def poll(self, state_since, events_since, map_since, actions_since):
        with self.lock:
            state = {
                key: {"v": item["v"], "t": item["t"]}
                for key, item in self.state.items()
                if item["seq"] > state_since
            }
            events = [e for e in self.events if e["seq"] > events_since][-EVENTS_PER_POLL:]
            objects = [o for o in self.map_objects.values() if o["seq"] > map_since]
            actions = {
                action_id: {k: v for k, v in action.items() if k not in ("handle", "seq")}
                for action_id, action in self.actions.items()
                if action["seq"] > actions_since
            }
            return {
                "seq": self.seq,
                "now": time.time(),
                "state": state,
                "events": events,
                "objects": objects,
                "actions": actions,
            }

    def run_command(self, name, command_id, args):
        command = (self.plugin.get("commands") or {}).get(name)
        if command is None or not command.get("ros"):
            return {"id": command_id, "status": "rejected", "error": f"unknown command '{name}'"}
        if not isinstance(command_id, str) or not command_id or len(command_id) > 64:
            return {"id": command_id, "status": "rejected", "error": "a command id is required"}

        # A retry after a lost reply must not run the command again: re-sending pump1 dispense would
        # restart the cycle and count the volume from zero.
        with self.lock:
            entry = self.command_results.get(command_id)
            first = entry is None
            if first:
                entry = {"done": threading.Event(), "result": None}
                self.command_results[command_id] = entry
                while len(self.command_results) > COMMAND_MEMORY:
                    self.command_results.popitem(last=False)
        if not first:
            entry["done"].wait(SERVICE_TIMEOUT_S + 2.0)
            return {**(entry["result"] or {"status": "error", "error": "still running"}), "duplicate": True}

        try:
            result = self._execute(name, command, command_id, args)
        except ArgError as exc:
            result = {"status": "rejected", "error": str(exc)}
        except Exception as exc:  # the caller must always get an answer
            self.get_logger().error(f"Command {name} failed: {exc}")
            result = {"status": "error", "error": str(exc)}
        result = {"id": command_id, "command": name, **result}
        entry["result"] = result
        entry["done"].set()
        if result["status"] in ("ok", "accepted"):
            self.get_logger().info(f"Command {name} {command_id[:8]}: {result['status']}")
        else:
            self.get_logger().warn(f"Command {name}: {result['status']}: {result.get('error')}")
        return result

    def _execute(self, name, command, command_id, args):
        if name in self.unavailable["commands"]:
            return {"status": "rejected", "error": self.unavailable["commands"][name]}
        cleaned = validate_args(command, args)
        ros = command["ros"]
        if "topic" in ros:
            return self._publish(name, ros, cleaned)
        if "service" in ros:
            return self._call_service(name, ros, cleaned)
        return self._send_goal(name, ros, cleaned, command_id)

    def _build(self, cls, ros, args):
        msg = cls()
        set_message_fields(msg, render_fields(ros.get("fields"), args))
        return msg

    def _publish(self, name, ros, args):
        entry = self.publishers_by_command[name]
        msg = self._build(entry["type"], ros, args)
        publisher = entry.get("publisher")
        oneshot = publisher is None
        if oneshot:
            # A long-lived transient-local publisher would hand this sample to any node that starts
            # later: after a restart of the mission node, an old target would be acted on at once.
            with self.lock:
                publisher = self.create_publisher(entry["type"], ros["topic"], entry["qos"])
        deadline = time.monotonic() + MATCH_WAIT_S
        while oneshot and publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        publisher.publish(msg)
        subscribers = publisher.get_subscription_count()
        if oneshot:
            timer = threading.Timer(ONESHOT_LINGER_S, self._destroy_publisher, args=(publisher,))
            timer.daemon = True
            timer.start()
        return {"status": "ok", "subscribers": subscribers}

    def _destroy_publisher(self, publisher):
        with self.lock:
            self.destroy_publisher(publisher)

    def _call_service(self, name, ros, args):
        entry = self.clients_by_command[name]
        if not entry["client"].service_is_ready():
            return {"status": "error", "error": f"service {ros['service']} is not available"}
        request = self._build(entry["type"].Request, ros, args)
        future = entry["client"].call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _future: done.set())
        if not done.wait(SERVICE_TIMEOUT_S):
            future.cancel()
            return {"status": "error", "error": "no reply from the service"}
        return {"status": "ok", "result": _message_to_dict(future.result())}

    def _send_goal(self, name, ros, args, command_id):
        entry = self.clients_by_command[name]
        if not entry["client"].server_is_ready():
            return {"status": "error", "error": f"action {ros['action']} is not available"}
        goal = self._build(entry["type"].Goal, ros, args)
        self._update_action(command_id, command=name, status="sending")

        def on_feedback(message):
            self._update_action(command_id, feedback=_message_to_dict(message.feedback))

        def on_result(future):
            result = future.result()
            self._update_action(
                command_id,
                status=_GOAL_STATUS.get(result.status, str(result.status)),
                result=_message_to_dict(result.result),
                handle=None,
            )

        def on_goal(future):
            handle = future.result()
            if not handle.accepted:
                self._update_action(command_id, status="rejected", handle=None)
                return
            self._update_action(command_id, status="running", handle=handle)
            handle.get_result_async().add_done_callback(on_result)

        entry["client"].send_goal_async(goal, feedback_callback=on_feedback).add_done_callback(
            on_goal
        )
        return {"status": "accepted", "action_id": command_id}

    def _update_action(self, action_id, **fields):
        with self.lock:
            self.seq += 1
            self.actions.setdefault(action_id, {}).update(fields, seq=self.seq)
            if len(self.actions) > COMMAND_MEMORY:
                self.actions.pop(next(iter(self.actions)))

    def cancel_action(self, action_id):
        with self.lock:
            action = self.actions.get(action_id)
            handle = action.get("handle") if action else None
        if handle is None:
            return {"id": action_id, "status": "rejected", "error": "no running action with this id"}
        handle.cancel_goal_async()
        return {"id": action_id, "status": "ok"}

    def image(self, name):
        """Returns (jpeg, etag), (None, None) if nothing has arrived yet, None for an unknown name."""
        entry = self.images.get(name)
        if entry is None:
            return None
        now = time.time()
        with self.lock:
            entry["requested"] = now
            if entry["sub"] is None:
                # Subscribed only while somebody looks: frames are not free even locally.
                def on_image(msg, entry=entry):
                    entry["msg"] = msg
                    entry["msg_seq"] += 1

                entry["sub"] = self.create_subscription(
                    entry["cls"],
                    entry["spec"]["topic"],
                    on_image,
                    _subscriber_qos(entry["spec"].get("qos")),
                    callback_group=self.group,
                )
            spec = entry["spec"]
            period = 1.0 / float(spec.get("max_hz", 0.5))
            stale = entry["msg_seq"] != entry["jpeg_seq"]
            if entry["msg"] is not None and stale and now - entry["jpeg_time"] >= period:
                entry["jpeg"] = _encode_jpeg(
                    entry["msg"], int(spec.get("max_width", 480)), int(spec.get("quality", 50))
                )
                entry["jpeg_seq"] = entry["msg_seq"]
                entry["jpeg_time"] = now
            if entry["jpeg"] is None:
                return None, None
            return entry["jpeg"], f'"{entry["jpeg_seq"]}"'

    def _drop_idle_images(self):
        now = time.time()
        with self.lock:
            for entry in self.images.values():
                if entry["sub"] is not None and now - entry["requested"] > IMAGE_IDLE_S:
                    self.destroy_subscription(entry["sub"])
                    entry["sub"] = None
                    entry["msg"] = None  # frames are large, do not keep the last one around


_GOAL_STATUS = {4: "succeeded", 5: "canceled", 6: "aborted"}


def _message_to_dict(msg):
    from rosidl_runtime_py import message_to_ordereddict

    return to_json_value_recursive(message_to_ordereddict(msg))


def to_json_value_recursive(value):
    if isinstance(value, dict):
        return {key: to_json_value_recursive(item) for key, item in value.items()}
    return to_json_value(value, 6)


def _encode_jpeg(msg, max_width, quality):
    # Imported here so that plugins without images do not need OpenCV on the drone.
    import cv2
    import numpy as np

    if hasattr(msg, "format"):  # sensor_msgs/CompressedImage
        frame = cv2.imdecode(np.frombuffer(bytes(msg.data), np.uint8), cv2.IMREAD_COLOR)
    else:
        from cv_bridge import CvBridge

        frame = CvBridge().imgmsg_to_cv2(msg, "bgr8")
    if frame is None:
        return None
    if frame.shape[1] > max_width:
        height = max(1, round(frame.shape[0] * max_width / frame.shape[1]))
        frame = cv2.resize(frame, (max_width, height), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return encoded.tobytes() if ok else None


def main(args=None):
    rclpy.init(args=args)
    node = BridgeNode()
    stopping = threading.Event()

    def serve():
        server = http_api.serve_forever(
            node,
            node.get_parameter("bind_address").value,
            int(node.get_parameter("port").value),
            node.get_logger(),
            stopping.is_set,
        )
        if server is not None:
            node.http_server = server
            server.serve_forever(poll_interval=0.5)

    threading.Thread(target=serve, daemon=True).start()

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        server = getattr(node, "http_server", None)
        if server is not None:
            server.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
