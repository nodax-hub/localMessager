import socket
import threading
import time

# Настройки
PORT = 12345
BUFFER_SIZE = 1024
HOST = '255.255.255.255'  # Расширенный адрес для проверки доступных устройств в сети

# Функция для прослушивания входящих сообщений (серверная часть)
def listen_for_messages():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('0.0.0.0', PORT))
    server_socket.listen(5)
    print(f"Стало хостом. Слушаю на порту {PORT}...")

    while True:
        client_socket, client_address = server_socket.accept()
        print(f"Подключение от {client_address}")

        while True:
            data = client_socket.recv(BUFFER_SIZE)
            if not data:
                break
            print(f"Сообщение от {client_address}: {data.decode()}")

        client_socket.close()

# Функция для отправки сообщений (клиентская часть)
def send_messages(target_host):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        client_socket.connect((target_host, PORT))
        print(f"Подключено к {target_host}")

        while True:
            message = input("Введите сообщение (или 'exit' для выхода): ")
            if message.lower() == 'exit':
                break
            client_socket.sendall(message.encode())
    except Exception as e:
        print(f"Ошибка подключения: {e}")
    finally:
        client_socket.close()

# Функция для проверки доступности хоста (сервер)
def check_if_host_exists():
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(2)  # Устанавливаем таймаут для подключения
        client_socket.connect((HOST, PORT))
        client_socket.close()
        return True  # Хост доступен
    except (socket.timeout, socket.error):
        return False  # Хост недоступен

# Основная логика
def main():
    # Шаг 1: Проверка, есть ли уже хост в сети
    print("Проверяю, доступен ли хост...")
    if check_if_host_exists():
        print("Хост найден! Подключаюсь как клиент.")
        target_host = input("Введите IP-адрес хоста: ")
        send_messages(target_host)
    else:
        print("Хост не найден. Становлюсь хостом.")
        # Запуск прослушивания входящих сообщений в отдельном потоке
        listener_thread = threading.Thread(target=listen_for_messages)
        listener_thread.daemon = True
        listener_thread.start()

        # Ожидание, пока программа не завершится
        while True:
            time.sleep(1)

if __name__ == "__main__":
    main()
