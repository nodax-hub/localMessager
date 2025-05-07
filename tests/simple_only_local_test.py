import asyncio
import json
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import pandas as pd

from AutoNet import AutoNet


class HeadlessNode:
    """Упрощённая обёртка для AutoNet без GUI.
    Позволяет посылать сообщения и получать обратные вызовы с отметками времени."""

    def __init__(self, name_suffix: str, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.id_suffix = name_suffix
        # статистика приема сообщений: (src_id, dst_id) -> List[float]
        self.latencies: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        # отметки времени отправленных пакетов msg_id -> (t_send, src_id, dst_id)
        self._pending: Dict[str, Tuple[float, str, str]] = {}

        # создаём AutoNet, задаём колбэки
        self.net = AutoNet(on_message=self._on_message,
                           on_up=lambda pid: None,
                           on_down=lambda pid: None)

        # заменяем сгенерированный UUID на предсказуемый ради удобства
        self.net.id = f"NODE_{self.id_suffix}"

    async def start(self):
        """Запуск сетевой части (WebSocket + Zeroconf) в рамках основного цикла."""
        # AutoNet::run содержит блокировку Event().wait(), поэтому стартуем отдельной задачей
        self._task = self.loop.create_task(self.net.run())

    async def stop(self):
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    # ---------------------------------------------------------------------
    #  Отправка сообщения с отметкой времени
    # ---------------------------------------------------------------------
    def send_probe(self, dst_pid: str, payload: dict):
        """Отправить сообщение и сохранить время отправки."""
        msg_id = payload.get("id")
        if msg_id is None:
            raise ValueError("payload должен содержать уникальное поле 'id'")

        now = time.perf_counter()
        self._pending[msg_id] = (now, self.net.id, dst_pid)

        # асинхронно планируем отправку (можно через broadcast, но нам нужен 1→1)
        asyncio.run_coroutine_threadsafe(
            self.net.conn.send(dst_pid, payload), self.loop)

    # ------------------------------------------------------------------
    #  Приём сообщения – считаем задержку
    # ------------------------------------------------------------------
    def _on_message(self, src_pid: str, raw: str):
        data = json.loads(raw)
        msg_id = data.get("id")
        t_now = time.perf_counter()

        # Отвечаем эхом, если не мы инициатор.
        if data.get("echo", False):
            # Это ответ: ищем соответствующую запись в pending
            if msg_id in self._pending:
                t_send, src, dst = self._pending.pop(msg_id)
                latency = t_now - t_send
                self.latencies[(src, dst)].append(latency)
        else:
            # Это входящий probe – шлём ответ
            response = {"id": msg_id, "echo": True}
            asyncio.run_coroutine_threadsafe(
                self.net.conn.send(src_pid, response), self.loop)


async def run_experiment(num_nodes: int = 3,
                        probes_per_pair: int = 5,
                        settle_time: float = 5.0) -> pd.DataFrame:
    """Создаёт num_nodes AutoNet-узлов в одном asyncio-цикле,
    ждёт формирования mesh-сети, затем шлёт взаимные probe-пакеты.
    Возвращает DataFrame со средними задержками."""

    loop = asyncio.get_running_loop()
    nodes: List[HeadlessNode] = [HeadlessNode(str(i), loop) for i in range(num_nodes)]

    # Запускаем все узлы
    for n in nodes:
        await n.start()

    # Даем сети устаканиться и установиться соединениям
    await asyncio.sleep(settle_time)

    # Список PID
    pids = [n.net.id for n in nodes]

    # Генерируем probe-сообщения
    msg_counter = 0
    for _ in range(probes_per_pair):
        for src in nodes:
            for dst_pid in pids:
                if dst_pid == src.net.id:
                    continue
                msg_counter += 1
                probe_msg = {"id": f"msg_{msg_counter}", "echo": False, "text": "probe"}
                src.send_probe(dst_pid, probe_msg)
                # чуть разведём во времени отправку, чтобы очереди не переполнились
                await asyncio.sleep(0.01)

    # ждём доставки последних ответов
    await asyncio.sleep(2.0)

    # Сводим статистику
    records = []
    for n in nodes:
        for (src, dst), lst in n.latencies.items():
            records.append({"src": src, "dst": dst, "samples": len(lst),
                            "mean_ms": 1000 * sum(lst) / len(lst)})

    df = pd.DataFrame(records)

    # Останавливаем всё
    for n in nodes:
        await n.stop()

    return df


def main():
    """Запускает эксперимент и сохраняет результаты в CSV и выводит таблицу."""
    df = asyncio.run(run_experiment(num_nodes=3, probes_per_pair=10))
    print("Результаты измерений задержек (мс):")
    print(df)

    # Сохраняем
    df.to_csv("latency_results.csv", index=False)
    print("Файл latency_results.csv сохранён.")


if __name__ == "__main__":
    main()
