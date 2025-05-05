"""
autonet_ws.py – односвязные каналы «peer ↔ peer» + Zeroconf discovery
Python 3.11+   websockets>=15   zeroconf>=0.146   ifaddr
pip install websockets zeroconf ifaddr
"""
from __future__ import annotations

import asyncio, json, logging, uuid
from dataclasses import dataclass
from typing import Dict, Optional

import ifaddr, ipaddress
from websockets.exceptions import ConnectionClosedError
from websockets.legacy.client import connect, WebSocketClientProtocol
from websockets.legacy.server import serve
from zeroconf import (
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    ServiceStateChange,
    Zeroconf,
)

# ---------- лог -------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
LOG = logging.getLogger("AutoNet")


def list_local_ips() -> list[str]:
    out: list[str] = []
    for ad in ifaddr.get_adapters():
        for ip in ad.ips:
            if not ip.is_IPv4:
                continue
            addr = ipaddress.ip_address(ip.ip)
            if addr.is_loopback or addr.is_link_local:
                continue
            out.append(ip.ip)
    return out or ["127.0.0.1"]


# ========================== Transport layer =================================
@dataclass
class Peer:
    ws: WebSocketClientProtocol
    online: bool = True


class ConnectionManager:
    """WebSocket-сервер + клиент; максимум 1 канал на каждый peer-id."""
    
    def __init__(self, peer_id: str, on_up, on_down, host: str = "0.0.0.0"):
        self.id, self.host = peer_id, host
        self.peers: Dict[str, Peer] = {}
        self._server = None
        self.port: Optional[int] = None
        self._on_up = on_up
        self._on_down = on_down
    
    async def start(self):
        self._server = await serve(
            self._on_accept, self.host, 0, ping_interval=10
        )
        self.port = self._server.sockets[0].getsockname()[1]
        LOG.info("listen ws://%s:%s", self.host, self.port)
    
    async def stop(self):
        for p in list(self.peers.values()):
            await p.ws.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
    
    async def connect(self, host: str, port: int, peer_id: str):
        if peer_id in self.peers:
            LOG.debug("skip dial → %s (already linked)", peer_id)
            return
        uri = f"ws://{host}:{port}"
        try:
            ws = await connect(uri, ping_interval=10)
        except OSError as e:
            LOG.warning("dial %s fail: %s", uri, e)
            return
        await ws.send(json.dumps({"peerId": self.id}))
        self.peers[peer_id] = Peer(ws)
        self._on_up(peer_id)
        LOG.info("connected → %s", peer_id)
        asyncio.create_task(self._recv_loop(peer_id, ws))
    
    async def send(self, peer_id: str, obj: dict):
        if peer_id in self.peers:
            await self.peers[peer_id].ws.send(json.dumps(obj))
    
    def broadcast(self, obj: dict):
        for pid in list(self.peers):
            asyncio.create_task(self.send(pid, obj))
    
    # --------------- internals ----------------------------------------------
    async def _on_accept(self, ws, _path):
        hdr = json.loads(await ws.recv())
        pid = hdr["peerId"]
        if pid in self.peers:  # дубликат
            LOG.info("duplicate incoming – close %s", pid)
            await ws.close()
            return
        self.peers[pid] = Peer(ws)
        self._on_up(pid)
        LOG.info("accepted ← %s", pid)
        await self._recv_loop(pid, ws)
    
    async def _recv_loop(self, pid: str, ws):
        try:
            async for txt in ws:
                LOG.info("«%s» ← %s", txt, pid)
        except ConnectionClosedError:
            pass
        finally:
            cur = self.peers.get(pid)
            if cur and cur.ws is ws:  # закрылся главный канал
                self.peers.pop(pid, None)
                LOG.warning("offline %s", pid)
                self._on_down(pid)
            else:  # это был лишний
                LOG.debug("dup socket of %s closed", pid)


# ====================== Zeroconf discovery ================================
class MdnsNode:
    _SERVICE = "_uavws._tcp.local."

    def __init__(self, autonet: "AutoNet", conn: ConnectionManager, loop):
        self.autonet = autonet
        self.conn = conn
        self.loop = loop
        self.id = autonet.id

        self.zc = Zeroconf(ip_version=IPVersion.All)
        self.info: ServiceInfo | None = None

    async def start(self):
        port = self.conn.port
        self.info = ServiceInfo(
            type_=self._SERVICE,
            name=f"{self.id}.{self._SERVICE}",
            addresses=[
                ipaddress.ip_address(ip).packed for ip in list_local_ips()
            ],
            port=port,
            properties={},
            server=f"{self.id}.local.",
        )
        await self.zc.async_register_service(self.info)
        ServiceBrowser(self.zc, self._SERVICE, handlers=[self._on_srv])
        LOG.info("mDNS publish on port %s", port)

    async def stop(self):
        if self.info:
            await self.zc.async_unregister_service(self.info)
        await self.zc._async_close()

    # -------- Zeroconf callback (4 позиционных аргумента!) -----------------
    def _on_srv(
        self,
        zeroconf,            # Zeroconf instance
        service_type,        # str
        name,                # full service name
        state_change,        # ServiceStateChange
    ):
        # интересуют только новые сервисы
        if (
            state_change is not ServiceStateChange.Added
            or (self.info and name == self.info.name)
        ):
            return

        info = zeroconf.get_service_info(service_type, name)
        if not info:
            return

        pid = name.split(".")[0]

        # младший peer-ID инициирует соединение
        if self.id > pid:
            return
        if (
            pid in self.autonet.connected_peers
            or pid in self.autonet.pending
        ):
            return

        host = ipaddress.ip_address(info.addresses[0]).exploded
        self.autonet.pending[pid] = (host, info.port, pid)
        self.loop.call_soon_threadsafe(
            asyncio.create_task,
            self.conn.connect(host, info.port, pid),
        )


# ============================ facade ========================================
class AutoNet:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.loop = None
        self.connected_peers: set[str] = set()
        self.pending: Dict[str, tuple[str, int, str]] = {}
        
        self.conn = ConnectionManager(self.id, self._peer_up, self._peer_down)
        self.mdns: Optional[MdnsNode] = None
    
    # --------- callbacks ----------------------------------------------------
    def _peer_up(self, pid: str):
        self.connected_peers.add(pid)
        self.pending.pop(pid, None)
        LOG.info("link UP ↔ %s  (total %d)", pid, len(self.connected_peers))
    
    def _peer_down(self, pid: str):
        self.connected_peers.discard(pid)
        LOG.warning("link DOWN ↔ %s (total %d)", pid, len(self.connected_peers))
    
    # ------------- life-cycle ----------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        await self.conn.start()
        self.mdns = MdnsNode(self, self.conn, self.loop)
        await self.mdns.start()
    
    async def stop(self):
        if self.mdns:
            await self.mdns.stop()
        await self.conn.stop()
    
    async def run(self):
        await self.start()
        try:
            while True:
                await asyncio.sleep(5)
                for pid in self.connected_peers:
                    await self.conn.send(pid, {"from": self.id, "text": "PING"})
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()


# ------------------ entry-point --------------------------------------------
if __name__ == "__main__":
    asyncio.run(AutoNet().run())
