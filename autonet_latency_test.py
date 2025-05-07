#!/usr/bin/env python
"""autonet_latency_test.py — распределённое измерение задержек AutoNet
---------------------------------------------------------------------
Скрипт запускается **на каждой машине** в одной локальной сети/VPN.
Один из экземпляров отмечается флагом `--coordinator`. Он

1. объявляет себя координатором (рассылает broadcast-сообщение типа
   `coord`);
2. после окончания теста собирает статистику от остальных узлов и
   формирует итоговый CSV / консольную таблицу.

Все прочие экземпляры работают в обычном режиме: участвуют в обмене
probe-пакетами, затем передают координатору накопленные latency-данные.

Пример запуска:
    # машина-координатор
    python autonet_latency_test.py --coordinator --expected 3 --probes 20

    # две другие машины (узлы-участники)
    python autonet_latency_test.py --expected 3 --probes 20

Аргументы
---------
--expected N   — ожидаемое число узлов в сети (включая себя и координатора)
--probes   M   — число probe-пакетов *от каждой пары* (по умолчанию 10)
--settle  S    — пауза (с) после старта до начала теста (по умолчанию 10)
--duration D   — общая длительность теста (по умолчанию 60)
--coordinator  — помечает данный узел как координатор

Результат
---------
Координатор сохраняет итоговую сводную таблицу `latency_results.csv`
и печатает её в консоль. Каждый не-координатор, кроме того, может
сохранить свой локальный CSV (`latency_{node_id}.csv`) — поведение
управляется флагом `--save-local`.
"""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import pandas as pd

from AutoNet import AutoNet


class LatencyNode:
    """Обёртка над AutoNet для распределённого эксперимента."""
    
    def __init__(self, is_coord: bool, expected: int, probes_per_pair: int,
                 settle: float, duration: float):
        # статистика от других узлов
        self._task = None
        self._foreign: List[dict] = []
        self.is_coord = is_coord
        self.expected = expected
        self.probes_per_pair = probes_per_pair
        self.settle = settle
        self.duration = duration
        
        # статистика задержек: (src, dst) -> list[latency]
        self.latencies: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        # ожидания ответа: msg_id -> (t_send, src, dst)
        self._pending: Dict[str, Tuple[float, str, str]] = {}
        # результаты, собранные координатором
        self._foreign_stats: List[Dict[str, str | int | float]] = []
        
        # AutoNet instance
        self.net = AutoNet(on_message=self._on_msg,
                           on_up=lambda pid: None,
                           on_down=lambda pid: None)
        # Бросаем явный лог для удобства
        print(f"[AUTO] запущен узел {self.net.id}  (координатор={self.is_coord})")
    
    # ------------------------------------------------------------------
    #  Запуск / остановка
    # ------------------------------------------------------------------
    async def start(self):
        self._task = asyncio.create_task(self.net.run())
    
    async def stop(self):
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
    
    # ------------------------------------------------------------------
    #  Приём сообщений
    # ------------------------------------------------------------------
    def _on_msg(self, src: str, raw: str):
        msg = json.loads(raw)
        t = msg.get("type")
        
        if t == "probe":  # обычный probe или echo
            if msg.get("echo"):
                mid = msg["id"]
                if mid in self._pending:
                    t0, s, d = self._pending.pop(mid)
                    self.latencies[(s, d)].append(time.perf_counter() - t0)
            else:
                echo = {"type": "probe", "id": msg["id"], "echo": True}
                asyncio.run_coroutine_threadsafe(
                    self.net.conn.send(src, echo), asyncio.get_event_loop())
        
        elif t == "coord" and not self.is_coord:
            self.coord_id = msg["coord_id"]
            print(f"[AUTO] coordinator = {self.coord_id}")
        
        elif t == "stats" and self.is_coord:
            self._foreign.extend(msg["records"])
            print(f"[AUTO] stats from {msg['from']}: {len(msg['records'])}")
    
    # ------------------------------------------------------------------
    #  Рассылка probe-сообщений
    # ------------------------------------------------------------------
    async def run_probes(self):
        await asyncio.sleep(self.settle)  # ждём установления сети
        pids = list(self.net.conn.peers.keys()) + [self.net.id]
        
        # Если сеть недоукомплектована — ждём доп.время, но не бесконечно
        extra_wait = 0
        max_time_wait = 30
        while len(pids) < self.expected:
            
            if extra_wait >= max_time_wait:
                print(f"[AUTO] Время, на ожидание других узлов {max_time_wait} секунд, вышло.")
                break
            
            pause_for = 2
            print("[AUTO] peers=", len(pids), f"< {self.expected}, пауза ещё на {pause_for} сек...")
            await asyncio.sleep(pause_for)
            extra_wait += pause_for
            pids = list(self.net.conn.peers.keys()) + [self.net.id]
        
        print(f"[AUTO] старт отправки probe: nPeers={len(pids)}, probes={self.probes_per_pair}")
        msg_counter = 0
        for _ in range(self.probes_per_pair):
            for dst_id in pids:
                if dst_id == self.net.id:
                    continue
                
                msg_counter += 1
                msg_id = f"{self.net.id}_{msg_counter}"
                probe = {"type": "probe", "id": msg_id}
                self._pending[msg_id] = (time.perf_counter(), self.net.id, dst_id)
                await self.net.conn.send(dst_id, probe)
                await asyncio.sleep(0.01)  # чуть разводим пакеты
        
        print("[AUTO] probe-фаза завершена")
    
    # ------------------------------------------------------------------
    #  Финальная отправка статистики координатору
    # ------------------------------------------------------------------
    def _local_records(self) -> List[dict]:
        recs = []
        for (src, dst), lst in self.latencies.items():
            recs.append({"src": src, "dst": dst,
                         "samples": len(lst),
                         "mean_ms": 1000 * sum(lst) / len(lst)})
        return recs
    
    async def send_stats(self):
        if self.is_coord:
            # координатор ничего никуда не шлёт
            return
        payload = {"type": "stats", "from": self.net.id,
                   "records": self._local_records()}
        await self.net.conn.broadcast(payload)
        print("[AUTO] статистика отправлена координатору")
    
    # ------------------------------------------------------------------
    #  Координатор: агрегация и вывод
    # ------------------------------------------------------------------
    def aggregate(self):
        # сводим собственные + пришедшие
        all_recs = self._local_records() + self._foreign_stats
        df = pd.DataFrame(all_recs)
        df.to_csv("latency_results.csv", index=False)
        print("\nИтоговая таблица (мс):\n", df)
        print("CSV сохранён в latency_results.csv")


# ====================================================================
#  main()
# ====================================================================

def parse_args():
    ap = argparse.ArgumentParser(
        description="Распределённое измерение задержек AutoNet")
    ap.add_argument("--expected", type=int, default=3,
                    help="Сколько всего узлов участвует, включая координатор")
    ap.add_argument("--probes", type=int, default=10,
                    help="Сколько probe-пакетов посылает каждая пара узлов")
    ap.add_argument("--settle", type=float, default=10,
                    help="Пауза (с) для установления сети перед началом теста")
    ap.add_argument("--duration", type=float, default=60,
                    help="Длительность теста в секундах, после чего узлы завершаются")
    ap.add_argument("--coordinator", action="store_true",
                    help="Запустить данный узел как координатор, собирающий результаты")
    return ap.parse_args()


async def main_async(args):
    node = LatencyNode(is_coord=args.coordinator,
                       expected=args.expected,
                       probes_per_pair=args.probes,
                       settle=args.settle,
                       duration=args.duration)
    await node.start()
    
    # если координатор, объявляемся
    if node.is_coord:
        await asyncio.sleep(3)  # небольшая пауза, чтобы mDNS успел разойтись
        await node.net.conn.broadcast({"type": "coord", "coord_id": node.net.id})
        print("[AUTO] координатор объявил себя")
    
    # запускаем probe-рассылку
    await node.run_probes()
    
    # ждём окончания общей длительности теста
    await asyncio.sleep(args.duration - args.settle)
    
    # отправляем свои статистики (если клиент)
    await node.send_stats()
    
    # координатор ждёт ещё 5 с, собирая чужие
    if node.is_coord:
        await asyncio.sleep(5)
        node.aggregate()
    
    await node.stop()


def main():
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
