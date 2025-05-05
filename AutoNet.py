import asyncio
import ipaddress
import json
import logging
import uuid
from dataclasses import dataclass
from itertools import chain
from typing import Dict, Optional

import ifaddr
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect, WebSocketClientProtocol
from websockets.legacy.server import serve, WebSocketServerProtocol
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo, AsyncServiceBrowser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("AutoNet")


def list_local_ips() -> dict[str, list[str]]:
    interfaces = {}
    for ad in ifaddr.get_adapters():
        ips = []
        for ip in ad.ips:
            if ip.is_IPv4:
                addr = ipaddress.ip_address(ip.ip)
                if not addr.is_loopback and not addr.is_link_local:
                    ips.append(ip.ip)
        if ips:
            interfaces[ad.nice_name] = ips
    return interfaces


@dataclass
class Peer:
    ws: WebSocketClientProtocol | WebSocketServerProtocol


class ConnectionManager:
    def __init__(self, me: str, on_up, on_down, host="0.0.0.0"):
        self.me, self.host = me, host
        self.port: int | None = None
        self.server: asyncio.AbstractServer | None = None
        self.peers: Dict[str, Peer] = {}
        self.known_peers: Dict[str, tuple[str, int]] = {}
        self._on_up, self._on_down = on_up, on_down
    
    async def start(self):
        self.server = await serve(self._accept, self.host, 0, ping_interval=15, ping_timeout=10, max_size=2 ** 20)
        self.port = self.server.sockets[0].getsockname()[1]
        LOG.info("WebSocket server listening on ws://0.0.0.0:%s (IPs %s)", self.port, list_local_ips())
    
    async def stop(self):
        for peer in list(self.peers.values()):
            await peer.ws.close(code=1001, reason="shutdown")
        if self.server:
            self.server.close()
            await self.server.wait_closed()
    
    async def connect(self, host: str, port: int, pid: str):
        if pid in self.peers:
            return
        self.known_peers[pid] = (host, port)
        try:
            ws = await connect(f"ws://{host}:{port}", ping_interval=15, ping_timeout=10, max_size=2 ** 20)
        except OSError as exc:
            LOG.warning("[DIAL] fail %s:%s – %s", host, port, exc)
            return
        await ws.send(json.dumps({"peerId": self.me}))
        await self._run_link(pid, ws, "[DIAL]")
    
    async def send(self, pid: str, obj: dict):
        peer = self.peers.get(pid)
        if peer:
            try:
                await peer.ws.send(json.dumps(obj))
            except ConnectionClosed:
                await self._drop_link(pid, peer.ws)
    
    def broadcast(self, obj: dict):
        payload = json.dumps(obj)
        for pid, peer in list(self.peers.items()):
            asyncio.create_task(self._safe_send(peer.ws, pid, payload))
    
    async def _safe_send(self, ws, pid, payload):
        try:
            await ws.send(payload)
        except ConnectionClosed:
            await self._drop_link(pid, ws)
    
    async def _accept(self, ws: WebSocketServerProtocol):
        try:
            hdr = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except asyncio.TimeoutError:
            await ws.close(code=1002, reason="handshake timeout")
            return
        pid = hdr.get("peerId")
        if pid in self.peers:
            await ws.close(code=1000, reason="duplicate")
            return
        await ws.send(json.dumps({"peerId": self.me, "ack": True}))
        await self._run_link(pid, ws, "[ACCEPT]")
    
    async def _run_link(self, pid: str, ws, tag: str):
        self.peers[pid] = Peer(ws)
        self._on_up(pid)
        LOG.info("%s link UP ↔ %s  (total %d)", tag, pid, len(self.peers))
        try:
            async for msg in ws:
                LOG.info("msg ← %s: %s", pid, msg)
        except ConnectionClosed as e:
            LOG.warning("link ERR ↔ %s  code=%s reason=%s", pid, e.code, e.reason)
        finally:
            await self._drop_link(pid, ws)
    
    async def _drop_link(self, pid: str, ws):
        if self.peers.get(pid) and self.peers[pid].ws is ws:
            self.peers.pop(pid, None)
            self._on_down(pid)
            LOG.warning("link DOWN ↔ %s  (total %d)", pid, len(self.peers))
    
    async def reconnect_peers(self):
        for pid, (host, port) in self.known_peers.items():
            if pid not in self.peers:
                await self.connect(host, port, pid)


class MdnsNode:
    _SERVICE = "_uavws._tcp.local."
    
    def __init__(self, app: "AutoNet", conn: ConnectionManager):
        self.app, self.conn = app, conn
        self.azc = AsyncZeroconf(ip_version=IPVersion.All)
        self.info: AsyncServiceInfo | None = None
        self.browser: AsyncServiceBrowser | None = None
    
    async def start(self):
        self.info = AsyncServiceInfo(
            type_=self._SERVICE,
            name=f"{self.app.id}.{self._SERVICE}",
            addresses=[ipaddress.ip_address(ip).packed for ip in chain.from_iterable(list_local_ips().values())],
            port=self.conn.port,
            server=f"{self.app.id}.local.",
        )
        await self.azc.async_register_service(self.info)
        self.browser = AsyncServiceBrowser(self.azc.zeroconf, self._SERVICE, handlers=[self._on_srv])
        LOG.info("[MDNS] publish async on port %s as %s", self.conn.port, self.info.name)
    
    async def stop(self):
        if self.info:
            await self.azc.async_unregister_service(self.info)
        if self.browser:
            await self.browser.async_cancel()
        await self.azc.async_close()
    
    def _on_srv(self, zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added and name != self.info.name:
            asyncio.create_task(self._handle_srv(zeroconf, service_type, name))
    
    async def _handle_srv(self, zeroconf, service_type, name):
        try:
            async_info = AsyncServiceInfo(service_type, name)
            await async_info.async_request(zeroconf, 3000)
            if async_info.addresses:
                pid = name.split(".")[0]
                
                if self.app.id > pid:
                    LOG.info("Пропускаем подключение к %s (наш peerId больше)", pid)
                    return
                
                host_ip = ipaddress.ip_address(async_info.addresses[0]).compressed
                
                # Найдём имя интерфейса:
                iface_name = next((iface for iface, ips in list_local_ips().items() if host_ip in ips), "неизвестный")
                
                LOG.info("[MDNS] Подключаемся к %s через интерфейс %s (%s)", pid, iface_name, host_ip)
                await self.conn.connect(host_ip, async_info.port, pid)
        except Exception as e:
            LOG.warning("Ошибка обработки mDNS: %s", e)


class AutoNet:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.conn = ConnectionManager(self.id, self._up, self._down)
        self.mdns: Optional[MdnsNode] = None
    
    def _up(self, pid: str):
        pass
    
    def _down(self, pid: str):
        pass
    
    async def monitor_interfaces(self):
        previous_interfaces = {}
        while True:
            current_interfaces = list_local_ips()
            
            # Интерфейсы, которые появились:
            new_interfaces = set(current_interfaces) - set(previous_interfaces)
            for iface in new_interfaces:
                LOG.info("[Сеть] Интерфейс подключён: %s (%s)", iface, current_interfaces[iface])
            
            # Интерфейсы, которые исчезли:
            lost_interfaces = set(previous_interfaces) - set(current_interfaces)
            for iface in lost_interfaces:
                LOG.warning("[Сеть] Интерфейс отключён: %s (%s)", iface, previous_interfaces[iface])
            
            # Если изменилось состояние – обновляем mDNS и соединения:
            if new_interfaces or lost_interfaces:
                previous_interfaces = current_interfaces
                all_ips = [ip for ips in current_interfaces.values() for ip in ips]
                if all_ips:
                    if self.mdns:
                        await self.mdns.stop()
                    self.mdns = MdnsNode(self, self.conn)
                    await self.mdns.start()
                    await self.conn.reconnect_peers()
                elif self.mdns:
                    await self.mdns.stop()
                    self.mdns = None
                    LOG.warning("[Сеть] Нет активных интерфейсов, MDNS остановлен")
            
            await asyncio.sleep(10)
    
    async def run(self):
        await self.conn.start()
        asyncio.create_task(self.monitor_interfaces())
        asyncio.create_task(self._heartbeat())
        while True:
            await asyncio.sleep(3600)
    
    async def _heartbeat(self):
        while True:
            await asyncio.sleep(5)
            self.conn.broadcast({"from": self.id, "text": "HELLO"})


if __name__ == "__main__":
    asyncio.run(AutoNet().run())
