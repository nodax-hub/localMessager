import abc
import asyncio
import enum
import json
import logging
import os
import platform
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List
from typing import Optional

import colorlog
from zeroconf import (
    Zeroconf,
    ServiceBrowser,
    ServiceInfo,
    ServiceStateChange,
)


# Настройка цветного логирования
def setup_logging():
    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'bold_red',
            }
        )
    )
    logger_ = colorlog.getLogger()
    logger_.setLevel(logging.DEBUG)
    logger_.addHandler(handler)
    return logger_


logger = setup_logging()


@dataclass(slots=True)
class Service:
    name: str
    address: Optional[str] = field(default=None, compare=False)
    port: Optional[int] = field(default=None, compare=False)

    def __str__(self):
        return f"{self.name} ({self.address}:{self.port})" if self.address and self.port else self.name


class EventType(enum.StrEnum):
    ADDED = "added"
    REMOVED = "removed"


# Интерфейс публикации сервисов
class ServicePublisherInterface(abc.ABC):
    @abc.abstractmethod
    def is_self(self, service: Service) -> bool:
        pass

    @abc.abstractmethod
    def register_service(self) -> None:
        pass

    @abc.abstractmethod
    def unregister_service(self) -> None:
        pass

    @property
    @abc.abstractmethod
    def ip(self) -> str:
        """Возвращает IP-адрес сервиса."""
        pass

    @property
    @abc.abstractmethod
    def service_name(self) -> str:
        """Возвращает имя сервиса."""
        pass


# Интерфейс обнаружения сервисов
class ServiceDiscoveryInterface(abc.ABC):
    @abc.abstractmethod
    def start_discovery(self) -> None:
        pass

    @abc.abstractmethod
    def stop_discovery(self) -> None:
        pass

    @abc.abstractmethod
    def register_listener(self, listener: Callable[[EventType, Service], None]) -> None:
        pass


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


class ZeroconfServiceDiscovery(ServiceDiscoveryInterface):
    def __init__(self, service_type: str):
        self.zeroconf = Zeroconf()
        self.service_type = service_type
        self.browser: Optional[ServiceBrowser] = None
        self.dispatcher = EventDispatcher()

        logger.debug(f"ZeroconfServiceDiscovery инициализирован для типа сервиса: {self.service_type}")

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


class ZeroconfServicePublisher(ServicePublisherInterface):
    def __init__(self, hostname: str, service_type: str, ip: str, port: int, service_name: str = None):
        self.zeroconf = Zeroconf()
        self._service_type = service_type
        self._service_name = service_name or f"{hostname}.{self._service_type}"

        self._ip = ip
        self._port = port
        self.hostname = hostname

        self.service_info: Optional[ServiceInfo] = None
        logger.debug(f"ZeroconfServicePublisher инициализирован для сервиса '{self._service_name}'.")

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


class MessageClient(abc.ABC):
    @abc.abstractmethod
    def send_text(self, client, message, sender):
        pass

    @abc.abstractmethod
    def send_file(self, client, file_path, sender):
        pass


class MessageServer(abc.ABC):
    @property
    @abc.abstractmethod
    def port(self):
        pass

    @abc.abstractmethod
    def start(self):
        pass

    @abc.abstractmethod
    def stop(self):
        pass


class SyncMessageServer(MessageServer):
    def __init__(self, host: str, port: int):
        self.host = host
        self._port = port

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.bind((self.host, self.port))

        self.server_socket.listen(5)

        self.running = False
        self.thread = threading.Thread(target=self.run, daemon=True)

        logger.debug(f"MessageServer инициализирован на {self.host}:{self.port}")

    def start(self):
        self.running = True
        self.thread.start()
        logger.info(f"MessageServer запущен на {self.host}:{self.port}")

    def stop(self):
        self.running = False
        try:
            self.server_socket.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии MessageServer: {e}")
        logger.info("MessageServer остановлен.")

    def run(self):
        while self.running:
            try:
                client_socket, addr = self.server_socket.accept()
                logger.info(f"Получено соединение от {addr}")

                handler_thread = threading.Thread(target=self.handle_client, args=(client_socket,), daemon=True)
                handler_thread.start()
            except Exception as e:
                if self.running:
                    logger.error(f"Ошибка в MessageServer: {e}")

    def handle_client(self, client_socket: socket.socket):
        with client_socket:
            try:
                data = client_socket.recv(4096).decode('utf-8')
                if not data:
                    return
                message = json.loads(data)
                self.process_message(message, client_socket)
            except Exception as e:
                logger.error(f"Ошибка при обработке клиента: {e}")

    @staticmethod
    def process_message(message: dict, client_socket: socket.socket):
        msg_type = message.get('type')

        if msg_type == 'text':
            sender = message.get('sender')
            content = message.get('content')
            logger.info(f"Сообщение от {sender}: {content}")
            print(f"Сообщение от {sender}: {content}")

        elif msg_type == 'file':
            sender = message.get('sender')
            filename = message.get('filename')
            filesize = message.get('filesize')
            logger.info(f"Получен файл '{filename}' от {sender} размером {filesize} байт.")
            print(f"Получен файл '{filename}' от {sender} размером {filesize} байт.")

            # Приём файла
            received_path = os.path.join('received_files', filename)
            os.makedirs(os.path.dirname(received_path), exist_ok=True)
            with open(received_path, 'wb') as f:
                remaining = filesize
                while remaining > 0:
                    chunk = client_socket.recv(min(4096, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            logger.info(f"Файл '{filename}' успешно сохранён по пути '{received_path}'.")
        else:
            logger.warning(f"Неизвестный тип сообщения: {msg_type}")

    @property
    def port(self):
        return self._port


class SyncMessageClient(MessageClient):
    def __init__(self):
        logger.debug("MessageClient инициализирован.")

    def send_text(self, target_service: Service, message: str, sender: str):
        data = {
            'type': 'text',
            'sender': sender,
            'content': message
        }
        self._send_data(target_service, data)

    def send_file(self, target_service: Service, file_path: str, sender: str):
        if not os.path.isfile(file_path):
            logger.error(f"Файл {file_path} не найден.")
            return

        filename = os.path.basename(file_path)
        filesize = os.path.getsize(file_path)
        data = {
            'type': 'file',
            'sender': sender,
            'filename': filename,
            'filesize': filesize
        }
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((target_service.address, target_service.port))
                sock.sendall(json.dumps(data).encode('utf-8'))
                logger.info(f"Отправка файла '{filename}' на {target_service.name}:{target_service.port}")
                with open(file_path, 'rb') as f:
                    while True:
                        bytes_read = f.read(4096)
                        if not bytes_read:
                            break
                        sock.sendall(bytes_read)
            logger.info(f"Файл '{filename}' успешно отправлен.")
        except Exception as e:
            logger.error(f"Ошибка при отправке файла: {e}")

    @staticmethod
    def _send_data(target_service: Service, data: dict):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((target_service.address, target_service.port))
                sock.sendall(json.dumps(data).encode('utf-8'))
                logger.info(f"Сообщение отправлено на {target_service.name}:{target_service.port}")
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения: {e}")


class DeviceDiscoveryManager:
    def __init__(self,
                 publisher: ServicePublisherInterface,
                 discovery: ServiceDiscoveryInterface,
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


def get_local_ip() -> Optional[str]:
    """Определяет локальный IP-адрес устройства."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Не обязательно подключаться, просто использовать для определения IP
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception as e:
        logger.error(f"Ошибка при определении локального IP: {e}")
        raise


# Пример использования
def main():
    received = Path('received_files')
    received.mkdir(parents=True, exist_ok=True)

    ip = get_local_ip()  # Получаем ip-адрес текущего устройства

    hostname = platform.node()
    service_type = "_sync._tcp.local."
    service_port = 8000  # Порт для zeroconf
    message_port = 8001  # Порт для сообщений

    publisher = ZeroconfServicePublisher(hostname=hostname,
                                         service_type=service_type,
                                         ip=ip,
                                         port=service_port)

    discovery = ZeroconfServiceDiscovery(service_type=service_type)

    message_client = SyncMessageClient()
    message_server = SyncMessageServer(host=ip, port=message_port)

    manager = DeviceDiscoveryManager(publisher=publisher,
                                     discovery=discovery,

                                     message_client=message_client,
                                     message_server=message_server,

                                     ignore_self=True)

    try:
        # Запускаем обнаружение сервисов и публикацию собственного сервиса
        manager.start()
        logger.info("Начато обнаружение сервисов. Нажмите Ctrl+C для остановки.")

        while True:
            command = input("Введите команду (msg <текст> | send <путь к файлу> | exit): ")

            if command.startswith(cmd := "msg"):
                message = command[len(cmd) + 1:]
                logger.debug(f"try send file {message}")
                manager.send_text_to_all(message)

            elif command.startswith(cmd := "send"):
                file_path = Path(command[len(cmd) + 1:]).resolve()
                logger.debug(f"try send file {file_path}")
                manager.send_file_to_all(file_path)

            elif command == "exit":
                break

            else:
                logger.warning("Неизвестная команда.")

    except KeyboardInterrupt:
        logger.info("\nОстановка обнаружения сервисов.")
    finally:
        # Останавливаем обнаружение и отзываем собственный сервис
        manager.stop()


if __name__ == "__main__":
    main()
