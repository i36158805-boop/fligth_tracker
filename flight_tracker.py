"""Трекер цен Ryanair с окном.

Что делает:
- раз в N минут спрашивает самые дешёвые рейсы на твои даты (библиотека ryanair-py);
- пишет историю цен в flight_prices.csv;
- шлёт сигнал (звук + Telegram), если цена ниже порога или упала на заданную сумму.

Установка:  pip install ryanair-py requests
Запуск:     python flight_tracker.py

Telegram (по желанию): переменные окружения TELEGRAM_TOKEN и TELEGRAM_CHAT_ID.
Важно: ryanair-py неофициальная. Не ставь интервал меньше 30 минут и сам следи за правилами сайта Ryanair.
Свободные места эта библиотека не показывает, только цены.
"""
import csv
import json
import os
import queue
import threading
import time
import tkinter as tk
from datetime import date, datetime, timedelta

SETTINGS_FILE = "flight_settings.json"
HISTORY_FILE = "flight_prices.csv"
ERROR_LOG = "tracker_errors.log"
MIN_INTERVAL = 30   # минут, чаще не разрешаем

BG = "#14161c"
CARD = "#1d212b"
LINE = "#2b3140"
FG = "#e8eaf0"
MUTED = "#8b93a7"
ACCENT = "#4f8cff"
OK = "#3ddc97"
WARN = "#ffb84d"
BAD = "#ff5c6c"


def default_settings():
    today = date.today()
    return {
        "origin": "",
        "destination": "",
        "date_from": (today + timedelta(days=7)).isoformat(),
        "date_to": (today + timedelta(days=37)).isoformat(),
        "round_trip": False,
        "ret_from": "",
        "ret_to": "",
        "currency": "EUR",
        "max_price": 0,
        "drop": 0,
        "interval_min": 180,
    }


# ---------- сервисные функции ----------

def log_error(where, err):
    """Ошибка в консоль и в файл, токен Telegram из текста вырезается."""
    detail = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}: {detail}"
    token = os.environ.get("TELEGRAM_TOKEN")
    if token:
        line = line.replace(token, "***")
    print(line, flush=True)
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_settings():
    cfg = default_settings()
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log_error("сохранение настроек", e)


def beep():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 300)
    except ImportError:
        print("\a", end="", flush=True)


def telegram(text):
    """True, если Telegram принял сообщение; False при ошибке; None, если ключи не заданы."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text},
            timeout=20,
        )
    except Exception as e:
        log_error("Telegram: запрос не ушёл", e)
        return False
    try:
        ok = resp.status_code == 200 and resp.json().get("ok") is True
    except ValueError:
        ok = False
    if not ok:
        log_error("Telegram", f"сервер ответил {resp.status_code}: {resp.text[:200]}")
    return ok


def parse_date(text, name):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{name}: дата должна быть в формате ГГГГ-ММ-ДД, например 2026-12-20")


def load_last_prices():
    """Последняя известная цена для каждого рейса из истории."""
    last = {}
    try:
        with open(HISTORY_FILE, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    last[row["key"]] = float(row["price"])
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return last


def append_history(rows):
    new_file = not os.path.exists(HISTORY_FILE)
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "key", "route", "when", "price", "currency"])
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            for r in rows:
                w.writerow([stamp, r["key"], r["route"], r["when"], r["price"], r["currency"]])
    except OSError as e:
        log_error("запись истории цен", e)


# ---------- график цен ----------

CHART_COLORS = ["#4f8cff", "#3ddc97", "#ffb84d", "#ff5c6c", "#b48cff", "#5fd4e8"]


def read_history():
    """История из CSV: {key: {label, currency, points: [(время, цена), ...]}}."""
    data = {}
    try:
        with open(HISTORY_FILE, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").timestamp()
                    price = float(row["price"])
                except (KeyError, ValueError):
                    continue
                item = data.setdefault(row["key"], {
                    "label": f"{row.get('route', '')}  {row.get('when', '')}",
                    "currency": row.get("currency", ""),
                    "points": [],
                })
                item["points"].append((ts, price))
    except OSError:
        pass
    for item in data.values():
        item["points"].sort()
    return data


def pick_series(data, limit=6):
    """Берёт рейсы с самой низкой последней ценой и раздаёт им цвета."""
    items = sorted(data.values(), key=lambda it: it["points"][-1][1])[:limit]
    for i, it in enumerate(items):
        it["color"] = CHART_COLORS[i % len(CHART_COLORS)]
    return items


def chart_ops(series, w, h):
    """Считает, что рисовать: линии, подписи, точки. Не зависит от tkinter, поэтому проверяется тестом."""
    left, right, top, bottom = 64, 20, 20, 44
    pw, ph = w - left - right, h - top - bottom
    pts_all = [p for s in series for p in s["points"]]
    if not pts_all:
        return []
    t0 = min(t for t, _ in pts_all)
    t1 = max(t for t, _ in pts_all)
    if t1 - t0 < 3600:                      # одна проверка или очень мало времени: расширяем окно
        mid = (t0 + t1) / 2
        t0, t1 = mid - 1800, mid + 1800
    p0 = min(p for _, p in pts_all)
    p1 = max(p for _, p in pts_all)
    pad = (p1 - p0) * 0.08 or 1.0
    p0, p1 = p0 - pad, p1 + pad

    def X(t):
        return left + (t - t0) / (t1 - t0) * pw

    def Y(p):
        return top + (p1 - p) / (p1 - p0) * ph

    ops = []
    for i in range(5):
        v = p0 + (p1 - p0) * i / 4
        ops.append(("line", [(left, Y(v)), (left + pw, Y(v))], "grid"))
        ops.append(("text", left - 8, Y(v), f"{v:.0f}", "e"))
    fmt = "%d.%m" if (t1 - t0) > 2 * 86400 else "%d.%m %H:%M"
    for i in range(5):
        t = t0 + (t1 - t0) * i / 4
        ops.append(("line", [(X(t), top), (X(t), top + ph)], "grid"))
        ops.append(("text", X(t), top + ph + 8, time.strftime(fmt, time.localtime(t)), "n"))
    for s in series:
        pts = [(X(t), Y(p)) for t, p in s["points"]]
        if len(pts) > 1:
            ops.append(("line", pts, s["color"]))
        for x, y in pts:
            ops.append(("dot", x, y, s["color"]))
    return ops


# ---------- трекер (работает в отдельном потоке) ----------

class Tracker(threading.Thread):
    def __init__(self, cfg, out, stop):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.out = out
        self.stop = stop
        self.last = {}

    def status(self, text, color):
        self.out.put(("status", text, color))

    def log(self, text):
        self.out.put(("log", f"[{time.strftime('%H:%M:%S')}] {text}"))

    def alert(self, text, color=BAD):
        self.status(text, color)
        self.log(text)
        beep()
        if telegram(text) is False:
            self.log("Telegram: сообщение не отправлено (подробности в tracker_errors.log)")

    def run(self):
        try:
            self.loop()
        except Exception as e:
            log_error("трекер", e)
            self.alert(f"Ошибка: {e}")
        finally:
            self.out.put(("done",))

    # --- настройки ---
    def parse(self):
        cfg = self.cfg
        origin = cfg["origin"].strip().upper()
        if len(origin) != 3:
            raise ValueError("«Откуда»: нужен код аэропорта из 3 букв, например DUB")
        dest = cfg["destination"].strip().upper()
        if dest and len(dest) != 3:
            raise ValueError("«Куда»: код аэропорта из 3 букв или пусто (тогда ищем везде)")
        d1 = parse_date(cfg["date_from"], "Даты «с»")
        d2 = parse_date(cfg["date_to"], "Даты «по»")
        if d2 < d1:
            raise ValueError("Даты «по» раньше, чем «с»")
        p = {"origin": origin, "dest": dest or None, "d1": d1, "d2": d2, "ret": None}
        if cfg["round_trip"]:
            r1 = parse_date(cfg["ret_from"], "Обратно «с»")
            r2 = parse_date(cfg["ret_to"], "Обратно «по»")
            if r2 < r1:
                raise ValueError("Обратно «по» раньше, чем «с»")
            p["ret"] = (r1, r2)
        return p

    # --- запрос цен ---
    def fetch(self, api, p):
        """Возвращает список рейсов: {key, route, when, price, currency}."""
        rows = []
        if p["ret"]:
            trips = api.get_cheapest_return_flights(
                p["origin"], p["d1"], p["d2"], p["ret"][0], p["ret"][1],
                destination_airport=p["dest"])
            for t in trips:
                out, back = t.outbound, t.inbound
                rows.append({
                    "key": f"RT|{out.origin}-{out.destination}|{out.departureTime:%Y-%m-%d}|{out.flightNumber}"
                           f"|{back.departureTime:%Y-%m-%d}|{back.flightNumber}",
                    "route": f"{out.origin}⇄{out.destination}",
                    "when": f"{out.departureTime:%d.%m} → {back.departureTime:%d.%m}",
                    "price": float(t.totalPrice),
                    "currency": getattr(out, "currency", self.cfg["currency"]),
                })
        else:
            flights = api.get_cheapest_flights(
                p["origin"], p["d1"], p["d2"], destination_airport=p["dest"])
            for f in flights:
                rows.append({
                    "key": f"OW|{f.origin}-{f.destination}|{f.departureTime:%Y-%m-%d}|{f.flightNumber}",
                    "route": f"{f.origin}→{f.destination}",
                    "when": f"{f.departureTime:%d.%m %H:%M}",
                    "price": float(f.price),
                    "currency": getattr(f, "currency", self.cfg["currency"]),
                })
        return rows

    # --- анализ ---
    def process(self, rows, n):
        cfg = self.cfg
        if not rows:
            self.log(f"Проверка {n}: рейсов на эти даты не найдено")
            return
        rows.sort(key=lambda r: r["price"])

        self.log(f"Проверка {n}: найдено рейсов: {len(rows)}. Самые дешёвые:")
        for r in rows[:5]:
            self.log(f"   {r['route']}  {r['when']}  {r['price']:.2f} {r['currency']}")

        try:
            max_price = float(cfg["max_price"])
            drop = float(cfg["drop"])
        except (TypeError, ValueError):
            max_price, drop = 0.0, 0.0

        alerts = []
        for r in rows:
            prev = self.last.get(r["key"])
            price = r["price"]
            if max_price > 0 and price <= max_price and (prev is None or prev > max_price or price < prev):
                alerts.append(f"Ниже порога: {r['route']} {r['when']} за {price:.2f} {r['currency']}")
            elif drop > 0 and prev is not None and prev - price >= drop:
                alerts.append(f"Цена упала: {r['route']} {r['when']}: {prev:.2f} → {price:.2f} {r['currency']}")

        append_history(rows)
        for r in rows:
            self.last[r["key"]] = r["price"]

        if alerts:
            self.alert("\n".join(alerts[:5]), OK)
            if len(alerts) > 5:
                self.log(f"И ещё {len(alerts) - 5} совпадений, см. flight_prices.csv")

    def loop(self):
        try:
            p = self.parse()
        except ValueError as e:
            self.alert(str(e), WARN)
            return
        try:
            from ryanair import Ryanair
        except ImportError:
            self.alert("Нет библиотеки ryanair-py. Выполни: pip install ryanair-py", WARN)
            return

        api = Ryanair(self.cfg["currency"])
        self.last = load_last_prices()
        interval = max(MIN_INTERVAL, int(self.cfg["interval_min"])) * 60
        n = 0
        fails = 0
        while not self.stop.is_set():
            n += 1
            self.status("Проверяю…", ACCENT)
            try:
                rows = self.fetch(api, p)
                fails = 0
            except Exception as e:
                fails += 1
                log_error("запрос цен", e)
                self.log(f"Не удалось получить цены ({fails}/3): {e}")
                if fails >= 3:
                    self.alert("3 неудачи подряд. Возможно, библиотека устарела или Ryanair блокирует запросы")
                    return
                self.stop.wait(300)
                continue

            self.process(rows, n)
            self.status("Жду следующую проверку", MUTED)
            self.stop.wait(interval)

        self.status("Остановлен", MUTED)
        self.log("Остановлено")


# ---------- окно ----------

class ChartWindow:
    W, H = 760, 400

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.title("График цен")
        self.win.configure(bg=BG)
        self.win.geometry("800x700")
        tk.Label(self.win, text="История цен", bg=BG, fg=FG,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=20, pady=(16, 0))
        self.note = tk.Label(self.win, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.note.pack(anchor="w", padx=20)
        self.canvas = tk.Canvas(self.win, width=self.W, height=self.H, bg=CARD,
                                highlightthickness=1, highlightbackground=LINE)
        self.canvas.pack(padx=20, pady=10)
        self.legend = tk.Text(self.win, bg=BG, fg=FG, relief="flat", height=8,
                              font=("Segoe UI", 10), state="disabled", wrap="word")
        self.legend.pack(fill="both", expand=True, padx=20)
        tk.Button(self.win, text="Обновить", command=self.draw, bg=ACCENT, fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"), cursor="hand2"
                  ).pack(pady=10, ipadx=16, ipady=4)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        self.legend.config(state="normal")
        self.legend.delete("1.0", "end")

        data = read_history()
        if not data:
            c.create_text(self.W / 2, self.H / 2, fill=MUTED, font=("Segoe UI", 11),
                          text="История пока пуста.\nЗапусти трекер и дождись первой проверки.")
            self.note.config(text="")
            self.legend.config(state="disabled")
            return

        series = pick_series(data)
        self.note.config(text=f"Показаны {len(series)} самых дешёвых рейсов из {len(data)}. "
                              f"Точка = одна проверка.")
        for op in chart_ops(series, self.W, self.H):
            if op[0] == "line":
                pts = [v for xy in op[1] for v in xy]
                if op[2] == "grid":
                    c.create_line(*pts, fill=LINE)
                else:
                    c.create_line(*pts, fill=op[2], width=2)
            elif op[0] == "text":
                c.create_text(op[1], op[2], text=op[3], anchor=op[4], fill=MUTED,
                              font=("Segoe UI", 8))
            elif op[0] == "dot":
                c.create_oval(op[1] - 3, op[2] - 3, op[1] + 3, op[2] + 3,
                              fill=op[3], outline=op[3])

        for i, s in enumerate(series):
            prices = [p for _, p in s["points"]]
            tag = f"c{i}"
            self.legend.tag_config(tag, foreground=s["color"])
            self.legend.insert("end", "■ ", tag)
            self.legend.insert(
                "end",
                f"{s['label']}: сейчас {prices[-1]:.2f} {s['currency']}, "
                f"мин {min(prices):.2f}, макс {max(prices):.2f}, проверок: {len(prices)}\n")
        self.legend.config(state="disabled")


class App:
    FIELDS = [
        ("origin", "Откуда (код, например DUB)"),
        ("destination", "Куда (код; пусто = везде)"),
        ("date_from", "Вылет с (ГГГГ-ММ-ДД)"),
        ("date_to", "Вылет по (ГГГГ-ММ-ДД)"),
        ("ret_from", "Обратно с (если туда-обратно)"),
        ("ret_to", "Обратно по"),
        ("currency", "Валюта"),
        ("max_price", "Сигнал, если цена ≤ (0 = выкл)"),
        ("drop", "Сигнал, если упала на ≥ (0 = выкл)"),
        ("interval_min", f"Проверять каждые, мин (от {MIN_INTERVAL})"),
    ]

    def __init__(self, root):
        self.root = root
        self.cfg = load_settings()
        self.out = queue.Queue()
        self.stop = threading.Event()

        root.title("Трекер билетов Ryanair")
        root.configure(bg=BG)
        root.geometry("580x850")
        root.minsize(540, 800)

        tk.Label(root, text="Трекер билетов", bg=BG, fg=FG,
                 font=("Segoe UI", 20, "bold")).pack(anchor="w", padx=24, pady=(20, 0))
        tk.Label(root, text="Следит за ценами Ryanair и пишет, когда они падают",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(0, 14))

        card = tk.Frame(root, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="x", padx=24)
        self.dot = tk.Label(card, text="●", bg=CARD, fg=MUTED, font=("Segoe UI", 22))
        self.dot.pack(side="left", padx=(16, 8), pady=14)
        self.state = tk.Label(card, text="Остановлен", bg=CARD, fg=FG,
                              font=("Segoe UI", 15, "bold"), wraplength=420, justify="left")
        self.state.pack(side="left")

        form = tk.Frame(root, bg=BG)
        form.pack(fill="x", padx=24, pady=(14, 0))
        form.columnconfigure(1, weight=1)
        self.vars = {}
        for i, (key, label) in enumerate(self.FIELDS):
            tk.Label(form, text=label, bg=BG, fg=MUTED,
                     font=("Segoe UI", 9)).grid(row=i, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(self.cfg[key]))
            self.vars[key] = var
            tk.Entry(form, textvariable=var, bg=CARD, fg=FG, insertbackground=FG,
                     relief="flat", highlightthickness=1, highlightbackground=LINE,
                     highlightcolor=ACCENT, font=("Segoe UI", 10)
                     ).grid(row=i, column=1, sticky="ew", padx=(12, 0), pady=4, ipady=5)

        self.round = tk.BooleanVar(value=bool(self.cfg["round_trip"]))
        tk.Checkbutton(root, text="Туда-обратно (тогда заполни даты «Обратно»)",
                       variable=self.round, bg=BG, fg=FG, selectcolor=CARD,
                       activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=20, pady=(8, 0))

        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", padx=24, pady=14)
        self.start_btn = tk.Button(row, text="Старт", command=self.start, bg=ACCENT, fg="white",
                                   relief="flat", font=("Segoe UI", 11, "bold"),
                                   activebackground=ACCENT, cursor="hand2")
        self.start_btn.pack(side="left", expand=True, fill="x", ipady=8, padx=(0, 6))
        self.stop_btn = tk.Button(row, text="Стоп", command=self.stop_tracker, bg=CARD, fg=FG,
                                  relief="flat", font=("Segoe UI", 11, "bold"),
                                  activebackground=LINE, cursor="hand2", state="disabled")
        self.stop_btn.pack(side="left", expand=True, fill="x", ipady=8, padx=6)
        tk.Button(row, text="График цен", command=lambda: ChartWindow(self.root), bg=CARD, fg=FG,
                  relief="flat", font=("Segoe UI", 11, "bold"), activebackground=LINE,
                  cursor="hand2").pack(side="left", expand=True, fill="x", ipady=8, padx=(6, 0))

        tk.Label(root, text="Журнал", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=24)
        self.logbox = tk.Text(root, bg=CARD, fg=FG, relief="flat", height=10,
                              highlightthickness=1, highlightbackground=LINE,
                              font=("Consolas", 9), state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=24, pady=(4, 20))

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def set_status(self, text, color):
        self.state.config(text=text)
        self.dot.config(fg=color)

    def add_log(self, text):
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def collect(self):
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        for key, default in (("max_price", 0.0), ("drop", 0.0)):
            try:
                cfg[key] = max(0.0, float(cfg[key].replace(",", ".")))
            except ValueError:
                cfg[key] = default
                self.vars[key].set("0")
        try:
            cfg["interval_min"] = max(MIN_INTERVAL, int(cfg["interval_min"]))
        except ValueError:
            cfg["interval_min"] = 180
        self.vars["interval_min"].set(str(cfg["interval_min"]))
        cfg["currency"] = cfg["currency"].upper() or "EUR"
        cfg["round_trip"] = self.round.get()
        return cfg

    def start(self):
        self.cfg = self.collect()
        save_settings(self.cfg)
        self.stop = threading.Event()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.add_log(f"[{time.strftime('%H:%M:%S')}] Запуск")
        Tracker(self.cfg, self.out, self.stop).start()

    def stop_tracker(self):
        self.stop.set()
        self.set_status("Останавливаю…", WARN)

    def poll(self):
        try:
            while True:
                msg = self.out.get_nowait()
                if msg[0] == "status":
                    self.set_status(msg[1], msg[2])
                elif msg[0] == "log":
                    self.add_log(msg[1])
                elif msg[0] == "done":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

    def close(self):
        self.stop.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
