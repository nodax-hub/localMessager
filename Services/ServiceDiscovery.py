import logging
import socket
import threading
from typing import Optional, Callable, List, Literal

from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

from models import Service

logger = logging.getLogger(__name__)


class ServiceDiscovery:
    def __init__(self,
                 service_type: str,
                 protocol: Literal["tcp", "udp"] = "tcp"):

        self.zeroconf = Zeroconf()
        self.service_type = f"_{service_type}._{protocol}.local."

        self.browser: Optional[ServiceBrowser] = None

        self._listeners: List[Callable[[ServiceStateChange, Service], None]] = []
        self._lock = threading.Lock()

    def start_discovery(self) -> None:
        self.browser = ServiceBrowser(
            self.zeroconf, self.service_type, handlers=[self.on_service_state_change]
        )
        logger.info("Запущено обнаружение сервисов.")

    def stop_discovery(self) -> None:
        if self.browser:
            self.browser.cancel()
            logger.info("Остановлено обнаружение сервисов.")
        self.zeroconf.close()
        logger.debug("Zeroconf закрыт.")

    def register_listener(self, listener: Callable[[ServiceStateChange, Service], None]) -> None:
        self._listeners.append(listener)
        logger.debug("Listener зарегистрирован для ServiceDiscovery.")

    def dispatch(self, event: ServiceStateChange, service: Service, log_message: str | None = None) -> None:
        logger.info(log_message)
        with self._lock:
            listeners_copy = self._listeners.copy()
        for listener in listeners_copy:
            try:
                listener(event, service)
            except Exception as e:
                logger.error(f"Ошибка в listener: {e}")

    def on_service_state_change(
            self,
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange) -> None:
        logger.debug(f"Состояние сервиса '{name}' изменилось на {state_change}.")

        match state_change:
            case ServiceStateChange.Added:
                info = zeroconf.get_service_info(service_type, name)
                if info and info.addresses:
                    address = socket.inet_ntoa(info.addresses[0])
                    port = info.port
                    service = Service(name=name, address=address, port=port)
                    self.dispatch(ServiceStateChange.Added, service,
                                  f"Обнаружен сервис: {name} по адресу {address}:{port}")
                else:
                    logger.warning(f"Не удалось получить информацию о сервисе: {name}")

            case ServiceStateChange.Updated:
                service = Service(name=name, address=None, port=None)
                self.dispatch(ServiceStateChange.Updated, service,
                              f"Сервис {name} обновлён")

            case ServiceStateChange.Removed:
                service = Service(name=name, address=None, port=None)
                self.dispatch(ServiceStateChange.Removed, service,
                              f"Сервис {name} удалён")

            case _:
                logger.warning(f"Необрабатываемое состояние сервиса: {state_change}")
