import socket
import threading

# Настройки
PORT = 12345
BUFFER_SIZE = 1024

# Функция для прослушивания входящих сообщений (серверная часть)
def listen_for_messages():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('0.0.0.0', PORT))
    server_socket.listen(5)
    print(f"Слушаю на порту {PORT}...")

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
def send_messages():
    target_host = input("Введите IP-адрес хоста для отправки сообщений: ")
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

def main():
    # Запуск прослушивания входящих сообщений в отдельном потоке
    listener_thread = threading.Thread(target=listen_for_messages)
    listener_thread.daemon = True
    listener_thread.start()

    # Основной поток для отправки сообщений
    send_messages()

if __name__ == "__main__":
    main()
