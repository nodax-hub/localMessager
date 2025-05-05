"""
autonet_ws.py  –  one-link peer↔peer over WebSocket + Zeroconf discovery
Python 3.11+          websockets>=15   zeroconf>=0.146   ifaddr
"""

from __future__ import annotations
import asyncio, json, logging, uuid, ipaddress, ifaddr
from dataclasses import dataclass
from typing import Dict, Optional

from websockets.asyncio.client import connect, ClientConnection
from websockets.asyncio.server import serve, ServerConnection
from websockets.exceptions import ConnectionClosed
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

# ───────────────  лог  ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("AutoNet")


# ───────────────  utils  ────────────────────────────────────────────────────
def list_local_ips() -> list[str]:
    out: list[str] = []
    for ad in ifaddr.get_adapters():
        for ip in ad.ips:
            if ip.is_IPv4:
                addr = ipaddress.ip_address(ip.ip)
                if not addr.is_loopback and not addr.is_link_local:
                    out.append(ip.ip)
    return out or ["127.0.0.1"]


# ───────────────  transport  ────────────────────────────────────────────────
@dataclass
class Peer:
    ws: ClientConnection | ServerConnection


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
        self.server = await serve(self._accept, self.host, 0, ping_interval=15)
        self.port = self.server.sockets[0].getsockname()[1]  # type: ignore[index]
        LOG.info("listen ws://0.0.0.0:%s  (IPs %s)", self.port, list_local_ips())
    
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
            ws = await connect(f"ws://{host}:{port}", ping_interval=15)
        except OSError as exc:
            LOG.warning("[DIAL] fail %s:%s – %s", host, port, exc)
            return
        await ws.send(json.dumps({"peerId": self.me}))
        await self._run_link(pid, ws, "[DIAL]")
    
    async def send(self, pid: str, obj: dict):
        peer = self.peers.get(pid)
        if peer:
            await peer.ws.send(json.dumps(obj))
    
    def broadcast(self, obj: dict):
        for pid in list(self.peers):
            asyncio.create_task(self.send(pid, obj))
    
    # ───────── внутреннее ─────────
    async def _accept(self, ws: ServerConnection):
        hdr = json.loads(await ws.recv())
        pid = hdr.get("peerId")
        if pid in self.peers:  # дубликат
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
        except ConnectionClosed as e:  # обрыв/тайм-аут/сетевой down
            code = getattr(e, "code", None)
            reason = getattr(e, "reason", None)
            if hasattr(e, "rcvd") and e.rcvd:
                code, reason = e.rcvd.code, e.rcvd.reason
            LOG.warning("link ERR ↔ %s  code=%s reason=%s", pid, code, reason)
        except Exception as exc:
            LOG.exception("link ERR ↔ %s  %s", pid, exc)
        finally:  # гарантированное закрытие
            if self.peers.get(pid) and self.peers[pid].ws is ws:
                self.peers.pop(pid, None)
                self._on_down(pid)


# ───────────────  Zeroconf  ────────────────────────────────────────────────
class MdnsNode:
    _SERVICE = "_uavws._tcp.local."
    
    def __init__(self, app: "AutoNet", conn: ConnectionManager):
        self.app, self.conn = app, conn
        self.loop = asyncio.get_running_loop()
        self.zc = Zeroconf(ip_version=IPVersion.All)
        self.info: ServiceInfo | None = None
    
    async def start(self):
        self.info = ServiceInfo(
            type_=self._SERVICE,
            name=f"{self.app.id}.{self._SERVICE}",
            addresses=[ipaddress.ip_address(ip).packed for ip in list_local_ips()],
            port=self.conn.port,
            server=f"{self.app.id}.local.",
        )
        await self.zc.async_register_service(self.info)
        ServiceBrowser(self.zc, self._SERVICE, handlers=[self._on_srv])
        LOG.info("[MDNS] publish on port %s", self.conn.port)
    
    async def stop(self):
        if self.info:
            await self.zc.async_unregister_service(self.info)
        await self.zc._async_close()
    
    # обработчик совместим с позиционным и именованным вызовом
    def _on_srv(self, *args, **kwargs):
        if kwargs:
            zc, svc_type, name, st = (
                kwargs["zeroconf"], kwargs["service_type"],
                kwargs["name"], kwargs["state_change"]
            )
        else:
            try:
                zc, svc_type, name, st = args[:4]
            except ValueError:
                return
        if st is not ServiceStateChange.Added:
            return
        if self.info and name == self.info.name:
            return
        info = zc.get_service_info(svc_type, name)  # type: ignore[arg-type]
        if not info:
            return
        pid = name.split(".")[0]
        if pid in self.conn.peers or self.app.id > pid:
            return
        host = ipaddress.ip_address(info.addresses[0]).compressed
        asyncio.run_coroutine_threadsafe(self.conn.connect(host, info.port, pid), self.loop)


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
        LOG.warning("link DOWN ↔ %s  (total %d)", pid, len(self.connected))
    
    async def run(self):
        LOG.info("my UUID: %s", self.id)
        await self.conn.start()
        self.mdns = MdnsNode(self, self.conn)
        await self.mdns.start()
        try:
            while True:
                await asyncio.sleep(5)
                self.conn.broadcast({"from": self.id, "text": "HELLO"})
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            if self.mdns:
                await self.mdns.stop()
            await self.conn.stop()


# ───────────────  entry point  ─────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(AutoNet().run())
