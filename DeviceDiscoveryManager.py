import platform
import subprocess
import threading
from pathlib import Path
from typing import List, Callable

from Messages.MessageClient import MessageClient
from Messages.MessageServer import MessageServer
from main import logger
from models import Service
from Services.ServiceDiscovery import ServiceDiscovery, EventType
from Services.ServicePublisher import ServicePublisher


class ConnectionChecker:
    def __init__(self, get_clients_callback: Callable[[], List[Service]], check_interval: int = 10):
        """
        :param get_clients_callback: Функция, возвращающая текущий список клиентов.
        :param check_interval: Интервал проверки в секундах.
        """
        self.get_clients_callback = get_clients_callback
        self.check_interval = check_interval
        self.running_event = threading.Event()  # Событие для управления остановкой
        self.running_event.set()  # Изначально событие установлено
        self.thread = threading.Thread(target=self.run, daemon=True)

        logger.debug("ConnectionChecker инициализирован.")

    def start(self):
        self.running_event.set()  # Устанавливаем событие для старта
        self.thread.start()
        logger.info("ConnectionChecker запущен.")

    def stop(self):
        self.running_event.clear()  # Сбрасываем событие, чтобы остановить поток
        logger.info("ConnectionChecker остановлен.")

    def run(self):
        while self.running_event.is_set():

            clients = self.get_clients_callback()
            for service in clients:
                if service.address:
                    reachable = self.ping(service.address)
                    status = "доступен" if reachable else "недоступен"
                    # logger.debug(f"Проверка подключения к {service.name} ({service.address}): {status}")

                    if not reachable:
                        logger.warning(f"Нет соединения с {service}")

            # Ждем указанное время перед следующей проверкой
            self.running_event.wait(self.check_interval)

    @staticmethod
    def ping(address: str) -> bool:
        """Проверяет доступность устройства по IP с помощью ICMP Echo."""
        try:
            # Используем системную утилиту ping
            param = '-n' if platform.system().lower() == 'windows' else '-c'
            command = ['ping', param, '1', address]
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result.returncode == 0

        except Exception as e:
            logger.error(f"Ошибка при пинге {address}: {e}")
            return False


class DeviceDiscoveryManager:
    def __init__(self,
                 publisher: ServicePublisher,
                 discovery: ServiceDiscovery,
                 message_client: MessageClient,
                 message_server: MessageServer,
                 ignore_self: bool = True):

        self.publisher = publisher
        self.discovery = discovery
        self.discovery.register_listener(self.handle_event)

        self.message_client = message_client
        self.message_server = message_server

        self._connection_checker = ConnectionChecker(self.get_active_clients, check_interval=5)

        self.ignore_self = ignore_self
        self.clients: List[Service] = []
        self.lock = threading.Lock()

    def start(self) -> None:
        self.publisher.register_service()
        self.discovery.start_discovery()
        self._connection_checker.start()
        self.message_server.start()

        logger.info("DeviceDiscoveryManager запущен.")

    def stop(self) -> None:
        self.message_server.stop()
        self.discovery.stop_discovery()
        self.publisher.unregister_service()
        self._connection_checker.stop()

        logger.info("DeviceDiscoveryManager остановлен.")

    def handle_event(self, event: EventType, service: Service) -> None:
        match event:
            case EventType.ADDED:
                self.add_client(service)

            case EventType.REMOVED:
                self.remove_client(service)

    def add_client(self, service: Service) -> None:
        with self.lock:
            if self.ignore_self and self.publisher.is_self(service):
                logger.info(f"Игнорируем собственный сервис: {service.name}.")
                return

            # Устанавливаем порт для сообщений
            service.port = self.message_server.port

            if service not in self.clients:
                self.clients.append(service)
                logger.info(f"Клиент добавлен: {service}")

    def remove_client(self, service: Service) -> None:
        with self.lock:
            self.clients = [c for c in self.clients if c.name != service.name]
            logger.info(f"Клиент удалён: {service}")

    def get_active_clients(self) -> List[Service]:
        with self.lock:
            return self.clients.copy()

    def send_text_to_all(self, message: str):
        sender = self.publisher.service_name

        for client in self.get_active_clients():
            self.message_client.send_text(client, message, sender)

    def send_file_to_all(self, file_path: Path):
        sender = self.publisher.service_name

        for client in self.get_active_clients():
            self.message_client.send_file(client, file_path, sender)
