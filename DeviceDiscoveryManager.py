import logging
import threading
from pathlib import Path
from typing import List

from zeroconf import ServiceStateChange

from Messages.MessageClient import MessageClient
from Messages.MessageServer import MessageServer
from Services.ServiceDiscovery import ServiceDiscovery
from Services.ServicePublisher import ServicePublisher
from models import Service

logger = logging.getLogger(__name__)


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

        self.ignore_self = ignore_self
        self.clients: List[Service] = []
        self.lock = threading.Lock()

    def start(self) -> None:
        self.publisher.register_service()
        self.discovery.start_discovery()
        self.message_server.start()

        logger.info("DeviceDiscoveryManager запущен.")

    def stop(self) -> None:
        self.message_server.stop()
        self.discovery.stop_discovery()
        self.publisher.unregister_service()

        logger.info("DeviceDiscoveryManager остановлен.")

    def handle_event(self, event: ServiceStateChange, service: Service) -> None:
        match event:
            case ServiceStateChange.Added:
                self.add_client(service)

            case ServiceStateChange.Removed:
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
