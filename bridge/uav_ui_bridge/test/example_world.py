#!/usr/bin/env python3
"""Feeds plugins/example/plugin.yaml with data on every declared kind of topic, service and action."""

import math
import time

import rclpy
from example_interfaces.action import Fibonacci
from example_interfaces.srv import AddTwoInts
from geometry_msgs.msg import Point, PoseArray, Pose, PoseStamped
from nav_msgs.msg import Path
from rclpy.action import ActionServer, CancelResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from sensor_msgs.msg import Image, NavSatFix
from std_msgs.msg import Bool, Float32MultiArray, Int32, String

LAT0, LON0 = 50.0598, 19.9172

try:
    from geographic_msgs.msg import GeoPath, GeoPointStamped, GeoPoseStamped
except ImportError:
    GeoPath = GeoPointStamped = GeoPoseStamped = None


class World(Node):
    def __init__(self):
        super().__init__("example_world")
        self.pubs = {
            name: self.create_publisher(cls, f"/example/{name}", 10)
            for name, cls in {
                "counter": Int32,
                "sensors": Float32MultiArray,
                "active": Bool,
                "origin": NavSatFix,
                "vehicle": NavSatFix,
                "navsat": NavSatFix,
                "local_pose": PoseStamped,
                "local_path": Path,
                "pose_array": PoseArray,
                "any": Point,
                "found": String,
                "image": Image,
            }.items()
        }
        if GeoPath:
            self.pubs["geopoint"] = self.create_publisher(GeoPointStamped, "/example/geopoint", 10)
            self.pubs["geopath"] = self.create_publisher(GeoPath, "/example/geopath", 10)
        self.received = {}
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        for topic, cls, depth in (
            ("say", String, 10),
            ("latched", String, qos),
            ("pose", PoseStamped, 10),
            ("goal", NavSatFix, 10),
        ):
            self.create_subscription(cls, f"/example/{topic}", self._logger_for(topic), depth)
        self.create_service(AddTwoInts, "/example/add", self._add)
        self.action_server = ActionServer(
            self,
            Fibonacci,
            "/example/fibonacci",
            self._fibonacci,
            cancel_callback=lambda _request: CancelResponse.ACCEPT,
        )
        self.tick = 0
        self.create_timer(0.5, self._publish)

    def _logger_for(self, topic):
        def callback(msg):
            self.get_logger().info(f"received on {topic}: {msg}"[:300])

        return callback

    def _add(self, request, response):
        response.sum = request.a + request.b
        return response

    def _fibonacci(self, goal_handle):
        sequence = [0, 1]
        for _ in range(goal_handle.request.order):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return Fibonacci.Result(sequence=sequence)
            sequence.append(sequence[-1] + sequence[-2])
            goal_handle.publish_feedback(Fibonacci.Feedback(sequence=sequence))
            time.sleep(0.3)
        goal_handle.succeed()
        return Fibonacci.Result(sequence=sequence)

    def _fix(self, lat, lon, frame=""):
        msg = NavSatFix(latitude=lat, longitude=lon, altitude=210.0)
        msg.header.frame_id = frame
        return msg

    def _publish(self):
        self.tick += 1
        t = self.tick * 0.05
        self.pubs["counter"].publish(Int32(data=self.tick))
        self.pubs["active"].publish(Bool(data=self.tick % 20 < 10))
        self.pubs["sensors"].publish(
            Float32MultiArray(data=[20.0 + 3.0 * math.sin(t), 7.0 + 0.5 * math.cos(t)])
        )
        self.pubs["origin"].publish(self._fix(LAT0, LON0))
        self.pubs["vehicle"].publish(
            self._fix(LAT0 + 0.0004 * math.sin(t), LON0 + 0.0006 * math.cos(t))
        )
        for index in range(2):
            self.pubs["navsat"].publish(
                self._fix(LAT0 + 0.0008 + index * 0.0002, LON0 - 0.0008, f"cel_{index}")
            )

        local = PoseStamped()
        local.pose.position.x, local.pose.position.y = 80.0, 60.0  # 80 m north, 60 m east
        self.pubs["local_pose"].publish(local)

        path = Path()
        for x, y in ((0.0, 0.0), (40.0, 0.0), (40.0, 40.0)):  # ENU: east, then north
            pose = PoseStamped()
            pose.pose.position.x, pose.pose.position.y = x, y
            path.poses.append(pose)
        self.pubs["local_path"].publish(path)

        array = PoseArray()
        for x, y in ((-40.0, 0.0), (-40.0, -40.0)):
            pose = Pose()
            pose.position.x, pose.position.y = x, y
            array.poses.append(pose)
        self.pubs["pose_array"].publish(array)

        self.pubs["any"].publish(Point(x=LAT0 - 0.0007, y=LON0 + 0.001, z=5.0))

        if self.tick % 10 == 0:
            self.pubs["found"].publish(String(data="obiekt"))
            self.get_logger().info("\x1b[92mZdarzenie testowe\x1b[0m")

        side = 64
        image = Image(height=side, width=side, encoding="bgr8", step=side * 3)
        shade = self.tick % 256
        image.data = bytes([shade, 80, 255 - shade] * (side * side))
        self.pubs["image"].publish(image)

        if GeoPath:
            point = GeoPointStamped()
            point.position.latitude, point.position.longitude = LAT0 - 0.001, LON0
            self.pubs["geopoint"].publish(point)
            geopath = GeoPath()
            for dlat, dlon in ((0.0, 0.0), (0.0003, 0.0006), (0.0, 0.0012)):
                pose = GeoPoseStamped()
                pose.pose.position.latitude = LAT0 - 0.0012 + dlat
                pose.pose.position.longitude = LON0 - 0.0006 + dlon
                geopath.poses.append(pose)
            self.pubs["geopath"].publish(geopath)


def main():
    rclpy.init()
    executor = MultiThreadedExecutor()
    executor.add_node(World())
    executor.spin()


if __name__ == "__main__":
    main()
