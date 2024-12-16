import enum
import socket
import threading
from typing import Optional, Callable, List

from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

from main import logger
from models import Service


class EventType(enum.StrEnum):
    ADDED = "added"
    REMOVED = "removed"


class ServiceDiscovery:
    def __init__(self, service_type: str):
        self.zeroconf = Zeroconf()
        self.service_type = service_type
        self.browser: Optional[ServiceBrowser] = None
        self.dispatcher = EventDispatcher()

        logger.debug(f"ServiceDiscovery инициализирован для типа сервиса: {self.service_type}")

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

    def register_listener(self, listener: Callable[[EventType, Service], None]) -> None:
        self.dispatcher.register_listener(listener)
        logger.debug("Listener зарегистрирован для ServiceDiscovery.")

    def on_service_state_change(
            self,
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
    ) -> None:
        logger.debug(f"Состояние сервиса '{name}' изменилось на {state_change}.")
        match state_change:
            case ServiceStateChange.Added:
                info = zeroconf.get_service_info(service_type, name)
                if info and info.addresses:
                    address = socket.inet_ntoa(info.addresses[0])
                    port = info.port
                    logger.info(f"Обнаружен сервис: {name} по адресу {address}:{port}")
                    service = Service(name=name, address=address, port=port)
                    self.dispatcher.dispatch(EventType.ADDED, service)

            case ServiceStateChange.Removed:
                service = Service(name=name, address=None, port=None)
                logger.info(f"Сервис {name} удалён")
                self.dispatcher.dispatch(EventType.REMOVED, service)

            case _:
                logger.warning("Не обрабатываемое состояние сервиса.")


class EventDispatcher:
    def __init__(self):
        self._listeners: List[Callable[[EventType, Service], None]] = []
        self._lock = threading.Lock()

    def register_listener(self, listener: Callable[[EventType, Service], None]) -> None:
        with self._lock:
            self._listeners.append(listener)
            logger.debug("Listener зарегистрирован в EventDispatcher.")

    def dispatch(self, event: EventType, service: Service) -> None:
        with self._lock:
            listeners_copy = self._listeners.copy()
        for listener in listeners_copy:
            try:
                listener(event, service)
            except Exception as e:
                logger.error(f"Ошибка в listener: {e}")
