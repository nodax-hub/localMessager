import os
import uuid
import logging
import platform
import socket
from pathlib import Path
from typing import Optional

import colorlog

from DeviceDiscoveryManager import DeviceDiscoveryManager
from Messages.MessageClient import MessageClient
from Messages.MessageServer import MessageServer
from NetworkInterface import NetworkInterface
from Services.ServiceDiscovery import ServiceDiscovery
from Services.ServicePublisher import ServicePublisher


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


def get_device_id(file_path_to_device_id=Path(".this_device_id.txt")):
    if not file_path_to_device_id.exists():
        file_path_to_device_id.write_text(str(uuid.uuid4()), encoding='utf-8')

    return file_path_to_device_id.read_text(encoding='utf-8').strip()


def get_interface() -> NetworkInterface:
    available_interfaces = NetworkInterface.list_interfaces()
    print("Доступные сетевые интерфейсы:")
    for i, iface in enumerate(available_interfaces, 1):
        print(f"{i}. {iface}")

    i = int(input(f"Выберите интерфейс через который необходимо выполнять связь: "))
    return NetworkInterface(available_interfaces[i - 1])


# Пример использования
def main():
    received = Path('received_files')
    received.mkdir(parents=True, exist_ok=True)

    device_id = get_device_id()
    print(f"Device ID: {device_id}")

    interface = get_interface()
    ip = interface.get_ip_v4_address()
    mac_address = interface.get_mac_address()
    print(f"Выбран интерфейс: '{interface.interface_name}', IP: {ip}, MAC: {mac_address}.")

    hostname = platform.node()
    service_type = "sync"
    service_name = f"{hostname}-{device_id}"

    message_server_port = 8001  # Порт для сообщений

    publisher = ServicePublisher(hostname=hostname,
                                 service_type=service_type,
                                 ip=ip,
                                 port=message_server_port,
                                 service_name=service_name,
                                 description={'MAC': mac_address,
                                              'DEVICE_ID': device_id,
                                              'INTERFACE_NAME': interface.interface_name})

    discovery = ServiceDiscovery(service_type=service_type)

    message_client = MessageClient()
    message_server = MessageServer(host=ip, port=message_server_port)

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
                manager.send_text_to_all(message)

            elif command.startswith(cmd := "send"):
                file_path = Path(command[len(cmd) + 1:]).resolve()
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
