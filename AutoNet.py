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
from itertools import cycle
from typing import Optional, Iterator, Tuple

import ifaddr
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect
from websockets.legacy.protocol import WebSocketCommonProtocol
from websockets.legacy.server import serve, WebSocketServerProtocol
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo, AsyncServiceBrowser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("AutoNet")


def get_list_available_interfaces() -> dict[str, list[str]]:
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
class InterfaceInfo:
    interface_name: str
    interface_ip: str
    network: str


def get_local_ipv4_subnets() -> list[dict]:
    """
    Возвращает список словарей с интерфейсной информацией и подсетью IPv4.
    """
    adapters = ifaddr.get_adapters()
    subnets = []
    
    for adapter in adapters:
        for ip in adapter.ips:
            # Пропускаем IPv6 адреса
            if isinstance(ip.ip, tuple):
                continue
            # Пропускаем link-local и loopback
            if ip.ip.startswith('169.254.') or ip.ip.startswith('127.'):
                continue
            try:
                network = ipaddress.ip_network(f"{ip.ip}/{ip.network_prefix}", strict=False)
                subnets.append({
                    'adapter': adapter,
                    'interface_ip': ip.ip,
                    'network': network
                })
            except ValueError:
                continue
    
    return subnets


def find_interface_for_ip(target_ip: str, subnets: list[dict] = None) -> InterfaceInfo | None:
    """
    Определяет интерфейс, через который доступен target_ip.
    """
    if subnets is None:
        subnets = get_local_ipv4_subnets()
    
    ip_obj = ipaddress.ip_address(target_ip)
    
    for subnet_info in subnets:
        network = subnet_info['network']
        if ip_obj in network:
            adapter = subnet_info['adapter']
            return InterfaceInfo(
                interface_name=adapter.nice_name,
                interface_ip=subnet_info['interface_ip'],
                network=str(network)
            )
    return None


def find_reachable_ips_with_interfaces(service_ips: list[str]) -> dict[str, InterfaceInfo]:
    """
    Возвращает словарь с IP и интерфейсной информацией для доступных адресов.
    """
    subnets = get_local_ipv4_subnets()
    results: dict[str, InterfaceInfo] = {}
    
    for ip_to_check in service_ips:
        interface_info = find_interface_for_ip(ip_to_check, subnets)
        if interface_info:
            results[ip_to_check] = interface_info
    
    return results


@dataclass
class Peer:
    pid: str
    ws: Optional[WebSocketCommonProtocol] = None  # действующее соединение (если есть)
    known_ips: set[str] = field(default_factory=set)  # «каталог» адресов для отладки/поключений
    fail_cnt: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # --- служебные кэш‑поля ---
    _address: Optional[str] = field(default=None, init=False, repr=False)
    _port: Optional[int] = field(default=None, init=False, repr=False)
    
    _filtered: list[str] = field(default_factory=list, init=False, repr=False)
    _ip_cycle: Optional[Iterator[str]] = field(default=None, init=False, repr=False)
    _dirty: bool = field(default=True, init=False, repr=False)  # пометка «список IP изменился»
    
    @property
    def address(self):
        if self._address is None:
            self.try_set_next_address()
        return self._address
    
    @property
    def port(self):
        return self._port
    
    @port.setter
    def port(self, value: int):
        self._port = value
    
    # ---------- состояние соединения ----------
    @property
    def connected(self) -> bool:
        """True, если WebSocket сейчас открыт."""
        return self.ws is not None and not self.ws.closed
    
    @property
    def local_endpoint(self) -> Optional[Tuple[str, int]]:
        """Интерфейс‑источник (ip, port), через который установлена текущая сессия."""
        if not self.connected:
            return None
        
        return getattr(self.ws, "local_address",
                       self.ws.transport.get_extra_info("sockname"))
    
    @property
    def via_interface(self) -> Optional[InterfaceInfo]:
        return find_interface_for_ip(self.address)
    
    @property
    def remote_endpoint(self) -> Optional[Tuple[str, int]]:
        """Удалённый адрес (ip, port), к которому мы подключены."""
        if not self.connected:
            return None
        
        return getattr(self.ws, "remote_address",
                       self.ws.transport.get_extra_info("peername"))
    
    # ---------- управление списком IP ----------
    def add_ip(self, *ips: str) -> None:
        """Добавить адрес(а) и пометить кэш как устаревший."""
        if any(ip not in self.known_ips for ip in ips):
            self.known_ips.update(ips)
            self._dirty = True
    
    def remove_ip(self, *ips: str) -> None:
        if any(ip in self.known_ips for ip in ips):
            for ip in ips:
                self.known_ips.discard(ip)
            self._dirty = True
    
    # ---------- фильтрация + циклический итератор ----------
    @classmethod
    def _is_valid_address(cls, addr: str) -> bool:
        """Валидатор"""
        return cls._is_ipv4(addr) and cls._found_corresponding_interface(addr)
    
    @staticmethod
    def _is_ipv4(addr):
        parts = addr.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    
    def _rebuild_cycle(self) -> None:
        """Фильтруем addresses → строим cycle → помечаем кэш «свежим»."""
        self._filtered = [ip for ip in self.known_ips if self._is_valid_address(ip)]
        self._ip_cycle = cycle(self._filtered) if self._filtered else None
        self._dirty = False
    
    def try_set_next_address(self) -> Optional[str]:
        """
        Отдать очередной IP:
        * если список менялся или цикла ещё нет — строим/перестраиваем;
        * возвращаем следующий адрес (или None, если нечего отдавать).
        """
        if not self.known_ips:
            self._address = None
        
        if self._dirty or self._ip_cycle is None:
            self._rebuild_cycle()
        
        if self._ip_cycle is None:  # после фильтрации ничего не подошло
            self._address = None
        
        self._address = next(self._ip_cycle)
        
        return self.address
    
    @classmethod
    def _found_corresponding_interface(cls, addr) -> bool:
        return find_interface_for_ip(addr) is not None


class ConnectionManager:
    def __init__(self,
                 me: str,
                 on_up,
                 on_down,
                 on_message=None,
                 heartbeat_msg='PING',
                 host="0.0.0.0"):
        
        self._heartbeat_msg = heartbeat_msg
        self.me, self.host = me, host
        self.port: int | None = None
        self.server: asyncio.AbstractServer | None = None
        self.peers: dict[str, Peer] = {}
        
        self._on_message = on_message
        self._on_up, self._on_down = on_up, on_down
    
    async def start(self):
        self.server = await serve(self._accept, self.host, 0, ping_interval=15, ping_timeout=10, max_size=2 ** 20)
        self.port = self.server.sockets[0].getsockname()[1]
        LOG.info("WebSocket server listening on ws://0.0.0.0:%s (IPs %s)", self.port, get_list_available_interfaces())
    
    async def stop(self):
        # корректно закрываем только «живые» WebSocket-объекты
        for peer in list(self.peers.values()):
            ws = getattr(peer, "ws", None)
            if ws is not None and not ws.closed:
                try:
                    await ws.close(code=1001, reason="shutdown")
                except ConnectionClosed:
                    # соединение уже умерло – игнорируем
                    pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()
    
    async def connect(self, host: str, port: int, pid: str):
        peer = self.peers.get(pid)
        
        if peer is None:
            LOG.warning(f"Not found Peer with {pid=}")
            return
        
        # Если уже существует активное соединение с Peer пропускаем подключение
        if peer.connected:
            return
        
        try:
            ws = await connect(f"ws://{host}:{port}", ping_interval=15, ping_timeout=10, max_size=2 ** 20)
        
        except OSError as exc:
            peer.fail_cnt[peer.address] += 1
            
            LOG.warning("[DIAL] fail %s:%s – %s (attempt %d)",
                        host, port, exc, peer.fail_cnt[peer.address])
            
            old = peer.address
            peer.try_set_next_address()
            
            if old != peer.address:
                LOG.info(f"[DIAL] После неудачных попыток подключения к {pid} сменили адрес {old} -> {peer.address}")
            
            else:
                count_max_retries = 3
                if max(peer.fail_cnt.values()) >= count_max_retries:
                    LOG.warning(f"[DIAL] Много неудачных попыток подключения к {pid} [{peer.address}:{peer.port}]")
                    # Возможно стоит принудительно перезапустить mDNS
                    self.multiple_errors_of_connection()
            
            return
        else:
            # успешное подключение — обнуляем счётчики
            for address, v in peer.fail_cnt.items():
                peer.fail_cnt[address] = 0
        
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
        
        for peer in self.peers.values():
            asyncio.create_task(self._safe_send(peer.ws, peer.pid, payload))
    
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
            if ws is not None:
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
        
        # TODO: вернуть нормальную проверку на дубли
        # if pid in self.peers:
        #     await ws.close(code=1000, reason="duplicate")
        #     return
        
        # Мини фикс (случай если к нам хотят подключиться раньше чем мы обнаружили этот сервис)
        if pid not in self.peers:
            self.peers[pid] = Peer(pid)
        
        await ws.send(json.dumps({"peerId": self.me, "ack": True}))
        await self._run_link(pid, ws, "[ACCEPT]")
    
    async def _run_link(self, pid: str, ws, tag: str):
        
        # Устанавливаем узлу текущий веб сокет
        peer = self.peers.get(pid)
        peer.ws = ws
        
        self._on_up(peer.pid)
        LOG.info("%s link UP ↔ %s", tag, peer.pid)
        
        try:
            async for msg in peer.ws:
                LOG.info("msg ← %s: %s", peer.pid, msg)
                
                if json.loads(msg).get('text', None) == self._heartbeat_msg:
                    LOG.info(f'Received heartbeat msg from {peer.pid}')
                    continue
                
                if self._on_message:
                    self._on_message(peer.pid, msg)
        
        except ConnectionClosed as e:
            if e.rcvd and e.rcvd.code == 1000:  # дубликат – не страшно
                LOG.debug("duplicate link closed ↔ %s", pid)
            
            else:
                LOG.warning("link ERR ↔ %s code=%s reason=%s", pid, e.code, e.reason)
        
        finally:
            await self._drop_link(pid, peer.ws)
    
    async def _drop_link(self, pid: str, ws):
        peer = self.peers.get(pid)
        
        if peer is not None and peer.ws is ws:
            peer.ws = None
            self._on_down(peer.pid)
            
            LOG.warning("link DOWN ↔ %s", peer.pid)
    
    async def reconnect_peers(self):
        for peer in self.peers.values():
            if not peer.connected:
                await self.connect(peer.address, peer.port, peer.pid)
    
    def multiple_errors_of_connection(self):
        pass


class MdnsNode:
    _SERVICE = "_uavws._tcp.local."
    
    def __init__(self, name: str, conn: ConnectionManager):
        self.pid, self.conn = name, conn
        self.azc = AsyncZeroconf(ip_version=IPVersion.All)
        self.info: AsyncServiceInfo | None = None
        self.browser: AsyncServiceBrowser | None = None
    
    async def start(self):
        self.info = AsyncServiceInfo(
            type_=self._SERVICE,
            name=f"{self.pid}.{self._SERVICE}",
            addresses=[ipaddress.ip_address(ip).packed for ip in
                       chain.from_iterable(get_list_available_interfaces().values())],
            port=self.conn.port,
            server=f"{self.pid}.local.",
        )
        
        # регистрируем свой сервис
        await self.azc.async_register_service(self.info)
        LOG.info("[MDNS] publish async on port %s as %s", self.conn.port, self.info.name)
        
        # Запускаем обнаружение других сервисов
        self.browser = AsyncServiceBrowser(self.azc.zeroconf, self._SERVICE, handlers=[self._on_srv])
    
    async def stop(self):
        if self.info:
            await self.azc.async_unregister_service(self.info)
        if self.browser:
            await self.browser.async_cancel()
        await self.azc.async_close()
    
    def _on_srv(self, zeroconf, service_type, name, state_change):
        if name == self.info.name:  # собственный анонс игнорируем
            LOG.info(f"Успешно обнаружили свой сервис {name}")
            return
        
        if state_change is ServiceStateChange.Added:
            # При обнаружении нового сервиса добавляем его в словарь известных узлов
            asyncio.create_task(self._handle_srv(zeroconf, service_type, name))
        
        elif state_change is ServiceStateChange.Updated:
            asyncio.create_task(self._handle_update(zeroconf, service_type, name))
        
        elif state_change is ServiceStateChange.Removed:
            asyncio.create_task(self._handle_lost(zeroconf, service_type, name))
    
    @staticmethod
    def _address_from_bytes(address):
        return ipaddress.ip_address(address).compressed
    
    @classmethod
    def _to_human_readable_addresses(cls, addresses) -> list[str]:
        return list(cls._address_from_bytes(a) for a in addresses)
    
    @staticmethod
    def _get_id_from_service_name(name):
        return name.split(".")[0]
    
    def _create_peer_by_service_name(self, name):
        pid = self._get_id_from_service_name(name)
        if pid not in self.conn.peers:
            self.conn.peers[pid] = Peer(pid=pid)
        else:
            LOG.warning(f"[MDNS] Уже имеется Peer с таким идентификатором: {pid}")
    
    def _remove_peer_by_service_name(self, name):
        pid = self._get_id_from_service_name(name)
        if pid in self.conn.peers:
            self.conn.peers.pop(pid, None)
            LOG.info("[MDNS] %s removed — stop reconnecting", pid)
        else:
            LOG.warning(f"[MDNS] Попытка удаления несуществующего сервиса {pid}")
    
    def _get_peer_by_service_name(self, name) -> Peer:
        pid = self._get_id_from_service_name(name)
        return self.conn.peers.get(pid)
    
    async def _handle_srv(self, zeroconf, service_type, name):
        # В первую очередь создаём Peer (добавляем в словарь)
        self._create_peer_by_service_name(name)
        
        try:
            async_info = AsyncServiceInfo(service_type, name)
            await async_info.async_request(zeroconf, 3000)
            
            if async_info.addresses:
                
                peer = self._get_peer_by_service_name(name)
                
                # Устанавливаем адреса и порт
                peer.add_ip(*(self._to_human_readable_addresses(async_info.addresses)))
                peer.port = async_info.port
                
                if peer.connected:
                    LOG.info("[MDNS] Уже есть живое соединение с %s – ничего делать не нужно", peer.pid)
                    return
                
                if self.pid > peer.pid:
                    LOG.info("[MDNS] Пропускаем подключение к %s (наш peerId больше)", peer.pid)
                    return
                
                LOG.info(f"[MDNS] Для {peer.pid} известны следующие адреса: {peer.known_ips}."
                         f" Подключаемся по адресу [{peer.address}:{peer.port}]")
                
                await self.conn.connect(peer.address, peer.port, peer.pid)
        
        except Exception as e:
            LOG.warning("Ошибка обработки mDNS: %s", e)
    
    async def _handle_update(self, zeroconf, service_type, name):
        try:
            async_info = AsyncServiceInfo(service_type, name)
            
            await async_info.async_request(zeroconf, 3000)
            if not async_info.addresses:
                return
            
            peer = self._get_peer_by_service_name(name)
            
            # Добавляем обновлённую информацию
            peer.add_ip(*(self._to_human_readable_addresses(async_info.addresses)))
            peer.port = async_info.port
        
        except Exception as e:
            LOG.warning("Ошибка обработки mDNS-update: %s", e)
    
    async def _handle_lost(self, zeroconf, service_type, name):
        self._remove_peer_by_service_name(name)


class AutoNet:
    def __init__(self, on_message=None, on_up=None, on_down=None):
        self.on_up = on_up
        self.on_down = on_down
        self.on_message = on_message
        self.id = uuid.uuid4().hex
        self._heartbeat_text = 'PING'
        
        self.conn = ConnectionManager(self.id, self.on_up, self.on_down, self.on_message, self._heartbeat_text)
        self.conn.multiple_errors_of_connection = lambda: self.reload_mdns()
        self.mdns: MdnsNode | None = None
    
    async def monitor_interfaces(self):
        previous_interfaces = {}
        while True:
            current_interfaces = get_list_available_interfaces()
            
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
                    self.mdns = MdnsNode(self.id, self.conn)
                    await self.mdns.start()
                
                elif self.mdns:
                    await self.mdns.stop()
                    self.mdns = None
                    LOG.warning("[Сеть] Нет активных интерфейсов, MDNS остановлен")
            
            await asyncio.sleep(10)
    
    async def run(self):
        await self.conn.start()
        self.mdns = MdnsNode(self.id, self.conn)
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
    
    async def reload_mdns(self):
        if self.mdns:
            await self.mdns.stop()  # ← отправит goodbye
            await self.mdns.start()


if __name__ == "__main__":
    asyncio.run(AutoNet().run())
