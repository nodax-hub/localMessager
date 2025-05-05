"""
autonet_ws_async.py – one-link peer↔peer over WebSocket + Async mDNS discovery
Python 3.11+          websockets>=15   zeroconf>=0.146   ifaddr
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

import ifaddr
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect, WebSocketClientProtocol
from websockets.legacy.server import serve, WebSocketServerProtocol
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import (
    AsyncZeroconf,
    AsyncServiceInfo,
    AsyncServiceBrowser,
)

# ───────────────  лог  ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("AutoNet")


# ───────────────  utils  ────────────────────────────────────────────────────
def list_local_ips() -> list[str]:
    ips: list[str] = []
    for ad in ifaddr.get_adapters():
        for ip in ad.ips:
            if ip.is_IPv4:
                addr = ipaddress.ip_address(ip.ip)
                if not addr.is_loopback and not addr.is_link_local:
                    ips.append(ip.ip)
    return ips or ["127.0.0.1"]


# ───────────────  transport  ────────────────────────────────────────────────
@dataclass
class Peer:
    ws: WebSocketClientProtocol | WebSocketServerProtocol


class ConnectionManager:
    """WebSocket-сервер + клиент, один активный канал на peer-id."""
    
    def __init__(self, me: str, on_up, on_down, host="0.0.0.0"):
        self.me, self.host = me, host
        self.port: int | None = None
        self.server: asyncio.AbstractServer | None = None
        self.peers: Dict[str, Peer] = {}
        self._on_up, self._on_down = on_up, on_down
    
    # ───────── публичный API ─────────
    async def start(self):
        self.server = await serve(
            self._accept,
            self.host,
            0,
            ping_interval=15,
            ping_timeout=10,  # быстрее сбрасываем «мёртвые» пиры :contentReference[oaicite:0]{index=0}
            max_size=2 ** 20,  # 1 МБ – защита от DoS
        )
        self.port = self.server.sockets[0].getsockname()[1]  # type: ignore[index]
        LOG.info(
            "WebSocket server listening on ws://0.0.0.0:%s (IPs %s)",
            self.port,
            list_local_ips(),
        )
    
    async def stop(self):
        for peer in list(self.peers.values()):
            await peer.ws.close(code=1001, reason="shutdown")
        if self.server:
            self.server.close()
            await self.server.wait_closed()
    
    async def connect(self, host: str, port: int, pid: str):
        if pid in self.peers:
            return
        try:
            ws = await connect(
                f"ws://{host}:{port}",
                ping_interval=15,
                ping_timeout=10,
                max_size=2 ** 20,
            )
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
        payload = json.dumps(obj)  # сериализуем один раз
        for pid, peer in list(self.peers.items()):
            asyncio.create_task(self._safe_send(peer.ws, pid, payload))
    
    # ───────── внутреннее ─────────
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
        if pid in self.peers:  # дубликат
            await ws.close(code=1000, reason="duplicate")
            return
        await ws.send(json.dumps({"peerId": self.me, "ack": True}))
        await self._run_link(pid, ws, "[ACCEPT]")
    
    async def _run_link(self, pid: str, ws: WebSocketServerProtocol | WebSocketClientProtocol, tag: str):
        self.peers[pid] = Peer(ws)
        self._on_up(pid)
        LOG.info("%s link UP ↔ %s  (total %d)", tag, pid, len(self.peers))
        try:
            async for msg in ws:
                LOG.info("msg ← %s: %s", pid, msg)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else ws.close_code  # новые поля :contentReference[oaicite:1]{index=1}
            reason = e.rcvd.reason if e.rcvd else ws.close_reason
            LOG.warning("link ERR ↔ %s  code=%s reason=%s", pid, code, reason)
        except Exception as exc:
            LOG.exception("link ERR ↔ %s  %s", pid, exc)
        finally:
            await self._drop_link(pid, ws)
    
    async def _drop_link(self, pid: str, ws):
        if self.peers.get(pid) and self.peers[pid].ws is ws:
            self.peers.pop(pid, None)
            self._on_down(pid)
            LOG.warning("link DOWN ↔ %s  (total %d)", pid, len(self.peers))


# ───────────────  Async Zeroconf  ───────────────────────────────────────────
class MdnsNode:
    """Async mDNS announcer / browser based on AsyncZeroconf."""
    
    _SERVICE = "_uavws._tcp.local."
    
    def __init__(self, app: "AutoNet", conn: ConnectionManager):
        self.app, self.conn = app, conn
        self.azc = AsyncZeroconf(ip_version=IPVersion.All)  # async-friendly :contentReference[oaicite:2]{index=2}
        self.info: AsyncServiceInfo | None = None
        self.browser: AsyncServiceBrowser | None = None
    
    async def start(self):
        self.info = AsyncServiceInfo(
            type_=self._SERVICE,
            name=f"{self.app.id}.{self._SERVICE}",
            addresses=[ipaddress.ip_address(ip).packed for ip in list_local_ips()],
            port=self.conn.port,
            server=f"{self.app.id}.local.",
        )
        await self.azc.async_register_service(self.info)
        self.browser = AsyncServiceBrowser(
            self.azc.zeroconf, self._SERVICE, handlers=[self._on_srv]
        )
        LOG.info("[MDNS] publish async on port %s as %s", self.conn.port, self.info.name)
    
    async def stop(self):
        if self.info:
            await self.azc.async_unregister_service(self.info)
        if self.browser:
            await self.browser.async_cancel()
        await self.azc.async_close()
    
    async def _handle_srv(self, zeroconf, service_type, name):
        """Асинхронная часть — тут можно await."""
        async_info = AsyncServiceInfo(service_type, name)
        await async_info.async_request(zeroconf, 3000)  # ждём до 3 с
        if not async_info.addresses:
            return
        pid = name.split(".")[0]
        if pid in self.conn.peers or self.app.id > pid:
            return
        host = ipaddress.ip_address(async_info.addresses[0]).compressed
        await self.conn.connect(host, async_info.port, pid)
    
    # ── ВАЖНО: этот метод теперь *синхронный* ──
    def _on_srv(self, zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        if self.info and name == self.info.name:
            return
        # планируем асинхронную часть в текущем loop
        asyncio.create_task(self._handle_srv(zeroconf, service_type, name))


# ───────────────  facade  ──────────────────────────────────────────────────
class AutoNet:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.connected: set[str] = set()
        self.conn = ConnectionManager(self.id, self._up, self._down)
        self.mdns: Optional[MdnsNode] = None
    
    def _up(self, pid: str):
        self.connected.add(pid)
    
    def _down(self, pid: str):
        self.connected.discard(pid)
    
    async def _heartbeat(self):
        while True:
            await asyncio.sleep(5)
            self.conn.broadcast({"from": self.id, "text": "HELLO"})
    
    async def run(self):
        LOG.info("Node ID: %s", self.id)
        async with asyncio.TaskGroup() as tg:  # Python 3.11+ TaskGroup
            await self.conn.start()
            self.mdns = MdnsNode(self, self.conn)
            await self.mdns.start()
            tg.create_task(self._heartbeat())


# ───────────────  entry point  ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(AutoNet().run())
    except KeyboardInterrupt:
        pass
