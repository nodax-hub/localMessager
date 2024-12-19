import psutil
import socket
import platform


class NetworkInterface:
    @staticmethod
    def list_interfaces() -> list[str]:
        """
        Возвращает список доступных сетевых интерфейсов.

        :return: Список названий интерфейсов.
        """
        return list(psutil.net_if_addrs().keys())

    def __init__(self, interface_name: str):
        """
        Инициализация класса с указанием имени сетевого интерфейса.

        :param interface_name: Название сетевого интерфейса (например, "eth0" или "wlan0").
        :raises ValueError: Если указанный интерфейс недоступен.
        """
        available_interfaces = self.list_interfaces()
        if interface_name not in available_interfaces:
            raise ValueError(f"Интерфейс '{interface_name}' недоступен. "
                             f"Доступные интерфейсы: {', '.join(available_interfaces)}")
        self.interface_name = interface_name

    def get_mac_address(self) -> str | None:
        """
        Возвращает MAC-адрес выбранного интерфейса.

        :return: MAC-адрес или None, если интерфейс не найден.
        """
        addrs = psutil.net_if_addrs().get(self.interface_name)
        if not addrs:
            return None

        for addr in addrs:
            # На Windows и других системах MAC-адрес определяется как AF_LINK
            if addr.family == psutil.AF_LINK or (platform.system() == "Windows" and addr.family == -1):
                return addr.address
        return None

    def _get_ip_address(self, family) -> str | None:
        """
        Возвращает IP-адрес для текущего интерфейса.

        :return: IP-адрес или None, если интерфейс не найден или не имеет IP.
        """
        addrs = psutil.net_if_addrs().get(self.interface_name)
        if not addrs:
            return None

        for addr in addrs:
            if addr.family == family:
                return addr.address
        return None

    def get_ip_v4_address(self):
        return self._get_ip_address(socket.AF_INET)

    def get_ip_v6_address(self):
        return self._get_ip_address(socket.AF_INET6)

    def get_interface_info(self) -> dict:
        """
        Возвращает информацию об интерфейсе, включая MAC- и IP-адреса.

        :return: Словарь с информацией об интерфейсе.
        """
        return {
            "interface_name": self.interface_name,
            "mac_address": self.get_mac_address(),
            "ipv4_address": self.get_ip_v4_address(),
            "ipv6_address": self.get_ip_v6_address(),
        }


def print_all_info():
    available_interfaces = NetworkInterface.list_interfaces()
    print("Доступные сетевые интерфейсы:")
    for i, iface in enumerate(available_interfaces, 1):
        print(f"{i}. {iface}")

    for i, iface in enumerate(available_interfaces, 1):
        print('\n', '-' * 100)
        print(f"{i}. {iface}")
        interface = NetworkInterface(iface)

        print("\nИнформация об интерфейсе:")
        for key, value in interface.get_interface_info().items():
            print(f"{key}: {value}")


# Пример использования
if __name__ == "__main__":
    print_all_info()
