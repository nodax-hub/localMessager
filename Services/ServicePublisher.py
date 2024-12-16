import socket
from typing import Optional

from zeroconf import Zeroconf, ServiceInfo

from main import logger
from models import Service


class ServicePublisher:
    def __init__(self, hostname: str, service_type: str, ip: str, port: int, service_name: str = None):
        self.zeroconf = Zeroconf()
        self._service_type = service_type
        self._service_name = service_name or f"{hostname}.{self._service_type}"

        self._ip = ip
        self._port = port
        self.hostname = hostname

        self.service_info: Optional[ServiceInfo] = None
        logger.debug(f"ServicePublisher инициализирован для сервиса '{self._service_name}'.")

    @property
    def ip(self) -> str:
        return self._ip

    @property
    def service_name(self) -> str:
        return self._service_name

    def is_self(self, service: Service) -> bool:
        return service.name == self._service_name

    def register_service(self) -> None:
        desc = {"path": "/"}  # Дополнительная информация о сервисе

        self.service_info = ServiceInfo(
            type_=self._service_type,
            name=self._service_name,
            addresses=[socket.inet_aton(self._ip)],
            port=self._port,
            properties=desc,
            server=f"{self.hostname}.local.",
        )

        self.zeroconf.register_service(self.service_info)
        logger.info(f"Сервис '{self._service_name}' зарегистрирован на {self._ip}:{self._port}.")

    def unregister_service(self) -> None:
        if self.service_info:
            self.zeroconf.unregister_service(self.service_info)
            logger.info(f"Сервис '{self._service_name}' отозван.")

        self.zeroconf.close()
        logger.debug("Zeroconf закрыт после отзыва сервиса.")
