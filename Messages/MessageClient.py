import json
import os
import socket

from main import logger
from models import Service


class MessageClient:
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
