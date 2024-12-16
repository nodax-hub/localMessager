import json
import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)
BUFFER_SIZE = 4 * 1024


class MessageServer:
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
                    raise

    def handle_client(self, client_socket: socket.socket):
        with client_socket:
            try:
                data = client_socket.recv(BUFFER_SIZE).decode('utf-8')
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
                    chunk = client_socket.recv(min(BUFFER_SIZE, remaining))
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
