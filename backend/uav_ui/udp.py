"""UDP forwarding: wfb-gs sends to one address, while the GUI and QGC both want the stream."""

import asyncio
import socket


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, on_datagram):
        self.on_datagram = on_datagram

    def datagram_received(self, data, addr):
        self.on_datagram(data, addr)

    def error_received(self, exc):
        pass  # ICMP "port unreachable" while QGC is closed is normal


async def _open(port, on_datagram):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Receiver(on_datagram),
        local_addr=("127.0.0.1", port),
        family=socket.AF_INET,
    )
    return transport


class UdpLink:
    """Listens on a port, remembers who sent to it, and optionally relays to a local program.

    Two-way, so QGC keeps working through the GUI: what QGC answers goes back to the original sender.
    """

    def __init__(self, listen_port, forward_port=None, on_packet=None):
        self.listen_port = listen_port
        self.forward_port = forward_port
        self.on_packet = on_packet
        self.peer = None
        self.packets = 0
        self.bytes = 0
        self._listener = None
        self._forwarder = None

    async def start(self):
        self._listener = await _open(self.listen_port, self._from_peer)
        if self.forward_port:
            self._forwarder = await _open(0, self._from_forward_target)

    def stop(self):
        for transport in (self._listener, self._forwarder):
            if transport is not None:
                transport.close()
        self._listener = self._forwarder = None

    def _from_peer(self, data, addr):
        self.peer = addr
        self.packets += 1
        self.bytes += len(data)
        if self.on_packet is not None:
            self.on_packet(data)
        if self._forwarder is not None:
            self._forwarder.sendto(data, ("127.0.0.1", self.forward_port))

    def _from_forward_target(self, data, _addr):
        self.send(data)

    def send(self, data):
        if self._listener is not None and self.peer is not None:
            self._listener.sendto(data, self.peer)
            return True
        return False


class VideoTee:
    """Copies RTP packets to MediaMTX and, optionally, to QGC."""

    def __init__(self, listen_port, sink_port, forward_port=None):
        self.listen_port = listen_port
        self.targets = [("127.0.0.1", sink_port)]
        if forward_port:
            self.targets.append(("127.0.0.1", forward_port))
        self.packets = 0
        self.bytes = 0
        self._listener = None
        self._sender = None

    async def start(self):
        self._listener = await _open(self.listen_port, self._on_packet)
        self._sender = await _open(0, lambda *_: None)

    def stop(self):
        for transport in (self._listener, self._sender):
            if transport is not None:
                transport.close()
        self._listener = self._sender = None

    def _on_packet(self, data, _addr):
        self.packets += 1
        self.bytes += len(data)
        for target in self.targets:
            self._sender.sendto(data, target)
