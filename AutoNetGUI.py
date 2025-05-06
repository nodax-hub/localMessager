import asyncio
import base64
import json
import pathlib
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

from AutoNet import AutoNet  # импортируем ваш движок


# ──────────────────────────────────────────────────────────────
#  Мост «tkinter  ⇄  asyncio»: сеть крутится в другом потоке,
#  в GUI попадают только события, безопасно переданные через Queue.
# ──────────────────────────────────────────────────────────────
class NetBridge:
    def __init__(self, ui):
        self.ui = ui
        self.loop = asyncio.new_event_loop()
        self.net = AutoNet(on_message=self._on_msg, on_up=self._peer_up, on_down=self._peer_down)
        threading.Thread(target=self._run_asyncio, daemon=True).start()
    
    def _run_asyncio(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.net.run())
    
    # ===========================================================
    # публичные методы, вызываемые из GUI‑потока
    # ===========================================================
    def send_text(self, pid, text):
        if pid == "_ALL_":
            asyncio.run_coroutine_threadsafe(
                self.net.conn.broadcast({"from": self.net.id, "text": text}),
                self.loop)
        else:
            asyncio.run_coroutine_threadsafe(
                self.net.conn.send(pid, {"from": self.net.id, "text": text}),
                self.loop)
    
    def send_file(self, pid, file_path):
        if pid == "_ALL_":
            asyncio.run_coroutine_threadsafe(
                self.net.conn.broadcast_file(file_path),
                self.loop)
        else:
            asyncio.run_coroutine_threadsafe(
                self.net.conn.send_file(pid, file_path),
                self.loop)
    
    # ===========================================================
    # колбэки сети
    # ===========================================================
    def _on_msg(self, pid, raw):
        """Вызывается в asyncio‑потоке, кладёт событие в очередь GUI."""
        self.ui.in_queue.put(("msg", pid, raw))
    
    def _peer_up(self, pid):
        self.ui.in_queue.put(("up", pid))
    
    def _peer_down(self, pid):
        self.ui.in_queue.put(("down", pid))


# ──────────────────────────────────────────────────────────────
#                            GUI
# ──────────────────────────────────────────────────────────────
class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AutoNet GUI")
        self.geometry("720x520")
        
        # очередь входящих событий из сети
        self.in_queue = queue.Queue()
        
        # сеть
        self.bridge = NetBridge(self)
        
        self.title(f"AutoNet GUI: {self.bridge.net.id}")
        
        # ======================================
        #  Список клиентов
        # ======================================
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=5, pady=5)
        ttk.Label(left, text="Клиенты").pack()
        self.peer_list = tk.Listbox(left, height=20)
        self.peer_list.pack(fill="y")
        self.peer_list.bind("<<ListboxSelect>>", self._on_select)
        
        # список всегда содержит спец‑строку «Все»
        self.peer_list.insert("end", "Все")  # индекс 0
        self.peer_ids = ["_ALL_"]  # параллельный список с pid
        
        # ======================================
        #  Панель подробностей
        # ======================================
        info = ttk.LabelFrame(left, text="Соединение")
        info.pack(fill="x", pady=5)
        self.info_iface = ttk.Label(info, text="—")
        self.info_addr = ttk.Label(info, text="—")
        self.info_port = ttk.Label(info, text="—")
        for w in (self.info_iface, self.info_addr, self.info_port):
            w.pack(anchor="w")
        
        # ======================================
        #  Центральный виджет – журнал
        # ======================================
        center = ttk.Frame(self)
        center.pack(side="top", fill="both", expand=True, padx=5, pady=5)
        self.log = scrolledtext.ScrolledText(center, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        
        # ======================================
        #  Нижняя панель ввода
        # ======================================
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=5, pady=5)
        self.entry = ttk.Entry(bottom)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._send_text())
        
        # ─── кнопки изначально неактивны ───────────────────────
        self.btn_send = ttk.Button(bottom, text="Отправить",
                                   command=self._send_text, state="disabled")
        self.btn_send.pack(side="left", padx=5)
        self.btn_file = ttk.Button(bottom, text="Файл…",
                                   command=self._send_file, state="disabled")
        self.btn_file.pack(side="left")
        
        # каждые 100 мс проверяем очередь
        self.after(100, self._poll_queue)
    
    # ───────────────────────────────────────
    #         обработчики GUI
    # ───────────────────────────────────────
    def _current_pid(self):
        sel = self.peer_list.curselection()
        if not sel:
            return "_ALL_"
        return self.peer_ids[sel[0]]
    
    def _send_text(self):
        text = self.entry.get().strip()
        if not text:
            return
        pid = self._current_pid()
        self.bridge.send_text(pid, text)
        self._append_log(f"Я → {pid}: {text}")
        self.entry.delete(0, "end")
    
    def _send_file(self):
        path = filedialog.askopenfilename()
        if not path:
            return
        pid = self._current_pid()
        self.bridge.send_file(pid, path)
        self._append_log(f"Файл {pathlib.Path(path).name} отправлен → {pid}")
    
    def _on_select(self, _evt):
        sel = self.peer_list.curselection()
        active = bool(sel)  # есть ли выбранная строка?
        
        # включаем/выключаем кнопки
        self.btn_send["state"] = "normal" if active else "disabled"
        self.btn_file["state"] = "normal" if active else "disabled"
        if not active:
            self._show_info("—", "—", "—")
            return
        
        idx = self.peer_list.curselection()
        if not idx:
            return
        
        pid = self.peer_ids[idx[0]]
        if pid == "_ALL_":
            self._show_info("—", "—", "—")
            return
        peer = self.bridge.net.conn.peers.get(pid)
        if peer and hasattr(peer, "info"):
            self._show_info(peer.info["iface"], peer.info["addr"], peer.info["port"])
    
    # ───────────────────────────────────────
    #         вспомогательные методы
    # ───────────────────────────────────────
    def _show_info(self, iface, addr, port):
        self.info_iface["text"] = f"Интерфейс: {iface}"
        self.info_addr["text"] = f"Адрес: {addr}"
        self.info_port["text"] = f"Порт: {port}"
    
    def _append_log(self, line):
        self.log["state"] = "normal"
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + line + "\n")
        self.log["state"] = "disabled"
        self.log.see("end")
    
    # ───────────────────────────────────────
    #   цикл опроса входящих событий из сети
    # ───────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                kind, *payload = self.in_queue.get_nowait()
                if kind == "msg":
                    pid, raw = payload
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        self._append_log(f"{pid} → ?  не‑JSON: {raw}")
                        continue
                    
                    if "text" in obj:
                        self._append_log(f"{pid} → Я: {obj['text']}")
                    elif "file" in obj and "data" in obj:
                        fn = obj["file"]
                        path = pathlib.Path("received") / pid / fn
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(base64.b64decode(obj["data"]))
                        self._append_log(f"{pid} прислал файл {fn}  → сохранён: {path}")
                
                elif kind == "up":
                    pid, = payload
                    if pid not in self.peer_ids:
                        self.peer_ids.append(pid)
                        self.peer_list.insert("end", pid)
                        self._append_log(f"⤒ подключился {pid}")
                
                elif kind == "down":
                    pid, = payload
                    if pid in self.peer_ids:
                        idx = self.peer_ids.index(pid)
                        self.peer_ids.pop(idx)
                        self.peer_list.delete(idx)
                        self._append_log(f"⤓ отключился {pid}")
                        # если уходящий клиент был выбран → снять выбор и выкл. кнопки
                        self._on_select(None)
        
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)


if __name__ == "__main__":
    MainWindow().mainloop()
