import asyncio
import base64
import ipaddress
import json
import logging
import pathlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
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
    info: dict = field(default_factory=dict)


class ConnectionManager:
    def __init__(self, me: str, on_up, on_down, on_message=None, heartbeat_msg='PING', host="0.0.0.0"):
        self._heartbeat_msg = heartbeat_msg
        self.on_message = on_message
        self.fail_cnt: defaultdict[str, int] = defaultdict(int)
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
            self.fail_cnt[pid] += 1
            LOG.warning("[DIAL] fail %s:%s – %s (attempt %d)",
                        host, port, exc, self.fail_cnt[pid])
            # после 3 подряд неудач убираем peer из кэша
            if self.fail_cnt[pid] >= 3:
                self.known_peers.pop(pid, None)
                self.fail_cnt.pop(pid, None)
                LOG.warning("[DIAL] giving up on %s — удаляем из known_peers", pid)
            return
        else:
            # успешное подключение — обнуляем счётчик
            self.fail_cnt.pop(pid, None)
        
        await ws.send(json.dumps({"peerId": self.me}))
        await self._run_link(pid, ws, "[DIAL]")
    
    async def send(self, pid: str, obj: dict):
        peer = self.peers.get(pid)
        if peer:
            try:
                await peer.ws.send(json.dumps(obj))
            except ConnectionClosed:
                await self._drop_link(pid, peer.ws)
    
    async def broadcast(self, obj: dict):
        payload = json.dumps(obj)
        for pid, peer in list(self.peers.items()):
            asyncio.create_task(self._safe_send(peer.ws, pid, payload))
    
    async def send_file(self, pid: str, file_path: str):
        data = pathlib.Path(file_path).read_bytes()
        payload = {
            "from": self.me,
            "file": pathlib.Path(file_path).name,
            "data": base64.b64encode(data).decode()
        }
        await self.send(pid, payload)
    
    async def broadcast_file(self, file_path: str):
        data = pathlib.Path(file_path).read_bytes()
        await self.broadcast({
            "from": self.me,
            "file": pathlib.Path(file_path).name,
            "data": base64.b64encode(data).decode()
        })
    
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
        # запоминаем подробности канала, чтобы GUI мог их показать
        host, port = ws.remote_address[:2]
        iface = next((n for n, ips in list_local_ips().items() if host in ips), "неизвестный")
        self.peers[pid].info = {"iface": iface, "addr": host, "port": port}
        
        self._on_up(pid)
        LOG.info("%s link UP ↔ %s  (total %d)", tag, pid, len(self.peers))
        try:
            async for msg in ws:
                LOG.info("msg ← %s: %s", pid, msg)
                
                if json.loads(msg).get('text', None) == self._heartbeat_msg:
                    LOG.info(f'Received heartbeat msg from {pid}')
                    continue
                    
                if self.on_message:
                    self.on_message(pid, msg)
                    
        except ConnectionClosed as e:
            if e.rcvd and e.rcvd.code == 1000:  # дубликат – не страшно
                LOG.debug("duplicate link closed ↔ %s", pid)
            else:
                LOG.warning("link ERR ↔ %s code=%s reason=%s", pid, e.code, e.reason)
        finally:
            await self._drop_link(pid, ws)
    
    async def _drop_link(self, pid: str, ws):
        if self.peers.get(pid) and self.peers[pid].ws is ws:
            self.peers.pop(pid, None)
            self._on_down(pid)
            LOG.warning("link DOWN ↔ %s  (total %d)", pid, len(self.peers))
    
    async def reconnect_peers(self):
        # снимаем «снимок» словаря, чтобы изменения внутри цикла не мешали
        for pid, (host, port) in list(self.known_peers.items()):
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
        if name == self.info.name:  # собственный анонс игнорируем
            return
        
        if state_change is ServiceStateChange.Added:
            asyncio.create_task(self._handle_srv(zeroconf, service_type, name))
        
        elif state_change is ServiceStateChange.Updated:
            asyncio.create_task(self._handle_update(zeroconf, service_type, name))
        
        elif state_change is ServiceStateChange.Removed:
            pid = name.split(".")[0]
            self.conn.known_peers.pop(pid, None)
            LOG.info("[MDNS] %s removed — stop reconnecting", pid)
    
    async def _handle_update(self, zeroconf, service_type, name):
        try:
            async_info = AsyncServiceInfo(service_type, name)
            await async_info.async_request(zeroconf, 3000)
            if not async_info.addresses:
                return
            
            pid = name.split(".")[0]
            host_ip = ipaddress.ip_address(async_info.addresses[0]).compressed
            new_port = async_info.port
            
            old = self.conn.known_peers.get(pid)
            if old and (old != (host_ip, new_port)):
                LOG.info("[MDNS] %s updated → %s:%s  (было %s:%s)",
                         pid, host_ip, new_port, *old)
                # обновляем кэш и, если соединения нет, пытаемся снова
                self.conn.known_peers[pid] = (host_ip, new_port)
                if pid not in self.conn.peers:
                    await self.conn.connect(host_ip, new_port, pid)
        
        except Exception as e:
            LOG.warning("Ошибка обработки mDNS-update: %s", e)
    
    async def _handle_srv(self, zeroconf, service_type, name):
        try:
            async_info = AsyncServiceInfo(service_type, name)
            await async_info.async_request(zeroconf, 3000)
            if async_info.addresses:
                pid = name.split(".")[0]
                
                if self.app.id > pid:
                    LOG.info("Пропускаем подключение к %s (наш peerId больше)", pid)
                    return
                
                if pid in self.conn.peers:
                    LOG.info("Уже есть живое соединение с %s – ничего делать не нужно", pid)
                    return
                
                host_ip = ipaddress.ip_address(async_info.addresses[0]).compressed
                
                # Найдём имя интерфейса:
                iface_name = next((iface for iface, ips in list_local_ips().items() if host_ip in ips), "неизвестный")
                
                LOG.info("[MDNS] Подключаемся к %s через интерфейс %s (%s)", pid, iface_name, host_ip)
                self.conn.known_peers[pid] = (host_ip, async_info.port)
                await self.conn.connect(host_ip, async_info.port, pid)
        except Exception as e:
            LOG.warning("Ошибка обработки mDNS: %s", e)


class AutoNet:
    def __init__(self, on_message=None, on_up=None, on_down=None):
        self.on_up = on_up
        self.on_down = on_down
        self.on_message = on_message
        self.id = uuid.uuid4().hex
        self._heartbeat_text = 'PING'
        self.conn = ConnectionManager(self.id, self.on_up, self.on_down, self.on_message, self._heartbeat_text)
        self.mdns: Optional[MdnsNode] = None
    
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
        self.mdns = MdnsNode(self, self.conn)
        await self.mdns.start()
        
        tasks = [
            asyncio.create_task(self.monitor_interfaces()),
            asyncio.create_task(self._heartbeat(self._heartbeat_text)),
            asyncio.create_task(self._keep_trying_peers()),
        ]
        try:
            await asyncio.Event().wait()  # run forever
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            for t in tasks:
                t.cancel()
            if self.mdns:
                await self.mdns.stop()  # ← отправит goodbye
            await self.conn.stop()
    
    async def _heartbeat(self, ping="PING"):
        while True:
            await asyncio.sleep(10)
            await self.conn.broadcast({"from": self.id, "text": ping})
    
    async def _keep_trying_peers(self):
        while True:
            await asyncio.sleep(15)  # плавный интервал
            await self.conn.reconnect_peers()


if __name__ == "__main__":
    asyncio.run(AutoNet().run())
