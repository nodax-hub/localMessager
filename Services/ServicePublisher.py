import logging
import socket
from typing import Optional, Literal

from zeroconf import Zeroconf, ServiceInfo

from models import Service

logger = logging.getLogger(__name__)


class ServicePublisher:
    def __init__(self,
                 hostname: str,
                 service_type: str,
                 ip: str,
                 port: int,
                 service_name: str = None,
                 description: dict = None,
                 protocol: Literal["tcp", "udp"] = "tcp"):
        self.zeroconf = Zeroconf()
        self._service_type = f"_{service_type}._{protocol}.local."
        self._service_name = f"{service_name}.{self._service_type}" or f"{hostname}.{self._service_type}"

        self._ip = ip
        self._port = port
        self.hostname = hostname

        self.description = {} if description is None else description

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

        self.service_info = ServiceInfo(
            type_=self._service_type,
            name=self._service_name,
            addresses=[socket.inet_aton(self._ip)],
            port=self._port,
            properties=self.description,
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
