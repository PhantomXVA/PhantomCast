"""LanAudio — отправитель звука Windows → Android по Wi-Fi.

Захват: WASAPI loopback (pyaudiowpatch), 48 кГц стерео (если устройство работает на другой частоте —
пересэмплирование soxr). Транспорт: UDP-пакеты по 2,5–10 мс, PCM s16 или Opus (libopus из PyAV).
Управление и статистика — TCP JSON-строки, телефон — сервер (см. README).

Запуск:  run.bat                         — GUI
         python lanaudio_sender.py --list    — устройства
         python lanaudio_sender.py --headless 192.168.100.3 --seconds 10 --source silence --codec opus
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import socket
import struct
import sys
import threading
import time

import numpy as np

RATE = 48000
CTRL_PORT = 50510
SERVICE_TYPE = "_lanaudio._tcp.local."
HDR = struct.Struct("<2sBBIII")  # magic, version, codec, session, seq, ts
CODEC_PCM, CODEC_OPUS = 0, 1
DEFAULT_DEVICE = "__default__"


# ============================================================ устройства

def wasapi_outputs(pa) -> list[str]:
    """Имена устройств вывода WASAPI, у которых есть loopback-двойник."""
    names = []
    for lb in pa.get_loopback_device_info_generator():
        names.append(lb["name"].replace(" [Loopback]", ""))
    return names


def find_loopback(pa, name: str | None) -> dict:
    if not name or name == DEFAULT_DEVICE:
        return pa.get_default_wasapi_loopback()
    for lb in pa.get_loopback_device_info_generator():
        if lb["name"].replace(" [Loopback]", "") == name:
            return lb
    raise RuntimeError(f"Устройство «{name}» не найдено")


def to_stereo(x: np.ndarray) -> np.ndarray:
    """(n, ch) float32 → (n, 2). 5.1/7.1 сводим в стерео, моно дублируем."""
    ch = x.shape[1]
    if ch == 2:
        return x
    if ch == 1:
        return np.repeat(x, 2, axis=1)
    k = 0.7071
    l = x[:, 0].copy(); r = x[:, 1].copy()
    if ch >= 3:
        l += k * x[:, 2]; r += k * x[:, 2]
    if ch >= 6:   # FL FR C LFE BL BR [SL SR]
        l += k * x[:, 4]; r += k * x[:, 5]
    if ch >= 8:
        l += k * x[:, 6]; r += k * x[:, 7]
    return np.stack([l, r], axis=1) * (0.5 if ch >= 3 else 1.0)


class Virtualizer:
    """5.1/7.1 → бинауральное стерео для наушников (упрощённая модель головы, без HRTF-баз).

    Для каждого канала-«динамика» (азимут θ) считаем сигнал для каждого уха КИХ-фильтром:
      · ITD — задержка дальнего уха (формула Вудворта, до ~0,65 мс),
      · ILD — дальнее ухо тише до −6 дБ,
      · «тень головы» — дальнее ухо теряет верха (срез 8 → 2,5 кГц),
      · задние источники — срез ~5 кГц и −2 дБ в обоих ушах (так мозг отличает «сзади» от «спереди»).
    Этого хватает, чтобы на слух различать, с какой стороны и спереди/сзади звук (шаги, выстрелы).
    """
    # порядок каналов WASAPI: FL FR C LFE BL BR SL SR; азимут в градусах, + вправо, 0 спереди
    ANGLES = {0: -30, 1: 30, 2: 0, 4: -110, 5: 110, 6: -90, 7: 90}
    TAPS = 96

    def __init__(self, ch: int, rate: int):
        self.ch = ch
        angles = dict(self.ANGLES)
        if ch == 8:
            angles[4], angles[5] = -150, 150   # 7.1: тыловые сзади, боковые по бокам
        self.angles = {i: a for i, a in angles.items() if i < ch}
        self.k = [(i, *self._kernels(a, rate)) for i, a in self.angles.items()]
        self.hist = np.zeros((self.TAPS - 1, ch), np.float32)

    @classmethod
    def _lowpass(cls, fc: float, rate: int, gain: float, delay: int) -> np.ndarray:
        n = 33
        t = np.arange(n) - n // 2
        fcn = min(0.49, fc / rate)
        h = np.sinc(2 * fcn * t) * np.hamming(n)
        h = h / h.sum() * gain
        out = np.zeros(cls.TAPS, np.float32)
        out[delay:delay + n] = h
        return out

    @classmethod
    def _kernels(cls, az: float, rate: int):
        th = np.radians(az)
        s = float(np.sin(th))                # >0 — источник справа
        lat = abs(s)
        rear = abs(az) > 90
        phi = float(np.arcsin(lat))
        itd = int(round(0.0875 / 343 * (phi + lat) * rate))   # задержка дальнего уха, отсчётов
        itd = min(itd, cls.TAPS - 34)
        near_fc = 5000 if rear else 16000
        near_g = 0.9 if rear else 1.0
        far_fc = (5000 if rear else 8000) - 5500 * lat * (0.5 if rear else 1.0) if lat > 0.01 else near_fc
        far_g = near_g * 10 ** (-6 * lat / 20)
        near = cls._lowpass(near_fc, rate, near_g, 0)
        far = cls._lowpass(far_fc, rate, far_g, itd)
        if s > 0:      # справа: правое ухо ближнее
            return far, near           # (левое, правое)
        if s < 0:
            return near, far
        return near, near

    def process(self, x: np.ndarray) -> np.ndarray:
        ext = np.concatenate([self.hist, x])
        self.hist = ext[-(self.TAPS - 1):].copy()
        n = len(x)
        l = np.zeros(n, np.float32); r = np.zeros(n, np.float32)
        for i, hl, hr in self.k:
            c = ext[:, i]
            l += np.convolve(c, hl, "valid")[:n]
            r += np.convolve(c, hr, "valid")[:n]
        if self.ch > 3:   # LFE (низкие) — одинаково в оба уха
            lfe = 0.35 * ext[self.TAPS - 1:, 3]
            l += lfe; r += lfe
        return np.stack([l, r], axis=1) * 0.55


# ============================================================ отправка

class Sender:
    """Одна «трансляция»: TCP-управление + UDP-звук. Сама переподключается, пока не вызван stop()."""

    def __init__(self, host: str, port: int = CTRL_PORT, device: str | None = None, codec: str = "pcm",
                 frame_ms: float = 5.0, bitrate: int = 256000, source: str = "loopback",
                 on_state=None, on_stats=None, surround: bool = False):
        self.surround = surround
        self.host, self.port = host, port
        self.device = device
        self.codec = codec
        self.frame_ms = frame_ms
        self.frames = int(round(RATE * frame_ms / 1000))
        self.bitrate = bitrate
        self.source = source
        self.on_state = on_state or (lambda s: None)
        self.on_stats = on_stats or (lambda d: None)
        self.peak = np.zeros(2, np.float32)  # для индикатора уровня (читает GUI)
        self.sent = 0
        self.capture_info = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---------------- жизненный цикл
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="lanaudio-sender")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def _run(self):
        first = True
        while not self._stop.is_set():
            try:
                self._session(first)
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self.on_state(f"Ошибка: {e}. Переподключение…")
            first = False
            self._stop.wait(0.5)
        self.on_state("Остановлено")

    # ---------------- одна сессия
    def _session(self, first: bool):
        self.on_state(f"Подключение к {self.host}…")
        ctl = socket.create_connection((self.host, self.port), timeout=4)
        ctl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        enc = None
        stream = pa = None
        udp = None
        try:
            session = random.getrandbits(32)
            hello = {"cmd": "hello", "codec": self.codec, "frame_ms": self.frame_ms, "session": session,
                     "name": socket.gethostname(), "rate": RATE, "ch": 2}
            if self.codec == "opus":
                enc = self._make_opus()
                hello["opus_head"] = base64.b64encode(bytes(enc.extradata or b"")).decode()
            try:  # keepalive: ОС сама заметит мёртвое соединение, но не рвёт при коротких заминках
                ctl.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                ctl.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
            except (OSError, AttributeError):
                pass
            ctl.sendall((json.dumps(hello) + "\n").encode())
            rf = ctl.makefile("r", encoding="utf-8")
            ctl.settimeout(10)
            reply = json.loads(rf.readline() or "{}")
            if not reply.get("ok"):
                raise RuntimeError(f"телефон отказал: {reply}")
            udp_port = int(reply["udp"])
            model = reply.get("model", "телефон")

            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:  # DSCP EF: на роутерах с WMM пакеты идут в очередь «голос»
                udp.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
            except OSError:
                pass
            udp.connect((self.host, udp_port))

            state = {"seq": 0, "ts": 0, "pending": np.zeros((0, 2), np.float32)}
            codec_id = CODEC_OPUS if enc else CODEC_PCM

            def send_block(st: np.ndarray):
                """st: (n, 2) float32 48 кГц. Режем на пакеты и шлём сразу — ничего не копим."""
                if len(state["pending"]):
                    st = np.concatenate([state["pending"], st])
                pos = 0
                n = self.frames
                while len(st) - pos >= n:
                    chunk = st[pos:pos + n]; pos += n
                    pcm = np.clip(chunk * 32767.0, -32768, 32767).astype("<i2")
                    if enc is not None:
                        payloads = self._encode_opus(enc, pcm, state["ts"])
                    else:
                        payloads = [pcm.tobytes()]
                    for p in payloads:
                        hdr = HDR.pack(b"LA", 1, codec_id, session, state["seq"] & 0xFFFFFFFF, state["ts"] & 0xFFFFFFFF)
                        try:
                            udp.send(hdr + p)
                        except OSError:
                            pass
                        state["seq"] += 1
                        self.sent += 1
                    state["ts"] += n
                state["pending"] = st[pos:]

            if self.source == "loopback":
                stream, pa = self._open_capture(send_block)
            else:
                threading.Thread(target=self._synth, args=(send_block,), daemon=True).start()

            self.on_state(f"Передаю на {model} · {self.capture_info}")
            # пинги в этом потоке, статистика — в отдельном
            alive = {"ok": True, "last": time.monotonic()}
            ctl.settimeout(None)  # читаем блокирующе: таймаут чтения не должен рвать сессию

            def reader():
                try:
                    for line in rf:
                        alive["last"] = time.monotonic()
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if "stats" in msg:
                            self.on_stats(msg["stats"])
                except (OSError, ValueError):
                    pass
                alive["ok"] = False

            threading.Thread(target=reader, daemon=True).start()
            while not self._stop.is_set() and alive["ok"]:
                if stream is not None and not stream.is_active():
                    raise RuntimeError("захват звука остановился")
                if time.monotonic() - alive["last"] > 15:  # телефон молчит (статистика идёт 2 раза в секунду)
                    raise RuntimeError("нет ответа от телефона")
                with self._lock:
                    ctl.sendall(b'{"cmd":"ping"}\n')
                self._stop.wait(1)
            if not self._stop.is_set():
                raise RuntimeError("телефон отключился")
            try:
                ctl.sendall(b'{"cmd":"bye"}\n')
            except OSError:
                pass
        finally:
            if stream is not None:
                try:
                    stream.stop_stream(); stream.close()
                except Exception:  # noqa: BLE001
                    pass
            if pa is not None:
                pa.terminate()
            self._synth_stop = True
            ctl.close()
            if udp:
                udp.close()
            self.on_stats(None)

    # ---------------- захват
    def _open_capture(self, send_block):
        import pyaudiowpatch as pyaudio
        import soxr
        pa = pyaudio.PyAudio()
        try:
            dev = find_loopback(pa, self.device)
        except Exception:
            pa.terminate()
            raise
        rate = int(dev["defaultSampleRate"])
        ch = int(dev["maxInputChannels"])
        rs = None if rate == RATE else soxr.ResampleStream(rate, RATE, 2, dtype="float32", quality="MQ")
        name = dev["name"].replace(" [Loopback]", "")
        virt = Virtualizer(ch, rate) if self.surround and ch >= 6 else None
        mode = " · 3D" if virt else (" · 3D недоступно: устройство стерео" if self.surround else "")
        self.capture_info = f"{name} · {rate} Гц {ch} кан." + ("" if rs is None else " → 48 кГц") + mode

        def cb(in_data, frame_count, time_info, status):
            try:
                x = np.frombuffer(in_data, np.float32).reshape(-1, ch)
                st = virt.process(x) if virt else to_stereo(x)
                if rs is not None:
                    st = rs.resample_chunk(np.ascontiguousarray(st))
                if len(st):
                    self.peak = np.maximum(self.peak, np.abs(st).max(axis=0))
                    send_block(st.astype(np.float32, copy=False))
            except Exception as e:  # noqa: BLE001
                print("capture:", e, file=sys.stderr)
            return (None, pyaudio.paContinue)

        stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                         input_device_index=dev["index"],
                         frames_per_buffer=max(64, int(rate * self.frame_ms / 1000)),
                         stream_callback=cb)
        stream.start_stream()
        return stream, pa

    def _synth(self, send_block):
        """Тестовый источник: тишина или тихий тон 440 Гц, в реальном темпе."""
        self._synth_stop = False
        self.capture_info = "тест: " + self.source
        n = self.frames
        t0 = time.perf_counter()
        k = 0
        while not self._stop.is_set() and not getattr(self, "_synth_stop", False):
            if self.source == "tone":
                t = (np.arange(n) + k * n) / RATE
                s = (0.05 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
                st = np.stack([s, s], axis=1)
            else:
                st = np.zeros((n, 2), np.float32)
            send_block(st)
            k += 1
            delay = t0 + k * n / RATE - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    # ---------------- Opus
    def _make_opus(self):
        import av
        enc = av.CodecContext.create("libopus", "w")
        enc.sample_rate = RATE
        enc.layout = "stereo"
        enc.format = "s16"
        enc.bit_rate = self.bitrate
        fd = {2.5: "2.5", 5.0: "5", 10.0: "10", 20.0: "20"}.get(self.frame_ms, "5")
        enc.options = {"application": "lowdelay", "frame_duration": fd, "vbr": "off"}
        enc.open()
        return enc

    def _encode_opus(self, enc, pcm: np.ndarray, ts: int) -> list[bytes]:
        import av
        fr = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="stereo")
        fr.sample_rate = RATE
        fr.pts = ts
        return [bytes(p) for p in enc.encode(fr)]


# ============================================================ headless / CLI

def cmd_list():
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    try:
        d = pa.get_default_wasapi_loopback()
        print("По умолчанию:", d["name"], int(d["defaultSampleRate"]), "Гц", d["maxInputChannels"], "кан.")
        for n in wasapi_outputs(pa):
            print(" -", n)
    finally:
        pa.terminate()


def cmd_headless(host: str, seconds: float, codec: str, frame_ms: float, source: str, device: str | None,
                 surround: bool = False) -> int:
    last = {}

    def st(d):
        if d:
            last.update(d)
            print("stats:", json.dumps(d, ensure_ascii=False))

    s = Sender(host, codec=codec, frame_ms=frame_ms, source=source, device=device, surround=surround,
               on_state=lambda m: print("state:", m), on_stats=st)
    s.start()
    time.sleep(seconds)
    s.stop()
    print(f"отправлено пакетов: {s.sent}")
    return 0 if last.get("packets", 0) > 0 else 1


# ============================================================ GUI

def run_gui() -> int:
    from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                   QListWidgetItem, QMainWindow, QPushButton, QSizePolicy, QVBoxLayout, QWidget)
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

    ACCENT = "#3b82f6"
    STYLE = f"""
    QWidget {{ background: #111318; color: #e6e8ee; font-family: 'Segoe UI'; font-size: 10pt; }}
    QLabel#title {{ font-size: 16pt; font-weight: 600; }}
    QLabel#section {{ color: #8b93a7; font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; margin-top: 8px; }}
    QLabel#hint {{ color: #8b93a7; font-size: 8.5pt; }}
    QLabel#stats {{ background: #171a21; border: 1px solid #262a33; border-radius: 8px; padding: 8px;
                    font-family: Consolas; font-size: 9pt; }}
    QListWidget {{ background: #171a21; border: 1px solid #262a33; border-radius: 8px; padding: 4px; outline: 0; }}
    QListWidget::item {{ padding: 8px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: #1f2a44; color: #fff; }}
    QListWidget::item:hover {{ background: #1a1f2b; }}
    QLineEdit, QComboBox {{ background: #171a21; border: 1px solid #262a33; border-radius: 6px; padding: 6px 8px; }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
    QComboBox QAbstractItemView {{ background: #171a21; selection-background-color: #1f2a44; }}
    QComboBox:disabled, QLineEdit:disabled, QListWidget:disabled {{ color: #5b6275; }}
    QPushButton {{ background: #222733; border: 0; border-radius: 8px; padding: 9px 14px; }}
    QPushButton:hover {{ background: #2a3040; }}
    QPushButton#primary {{ background: {ACCENT}; color: white; font-weight: 600; font-size: 11pt; padding: 12px; }}
    QPushButton#primary:hover {{ background: #2563eb; }}
    QPushButton#danger {{ background: #ef4444; color: white; font-weight: 600; font-size: 11pt; padding: 12px; }}
    """

    CODECS = [
        ("Без потерь — PCM 16 бит (1,5 Мбит/с)", "pcm", 0),
        ("Сжатый — Opus 320 кбит/с", "opus", 320000),
        ("Сжатый — Opus 192 кбит/с", "opus", 192000),
        ("Сжатый — Opus 128 кбит/с", "opus", 128000),
    ]
    FRAMES = [("2,5 мс — минимум задержки", 2.5), ("5 мс — рекомендуется", 5.0), ("10 мс — для слабого Wi-Fi", 10.0)]

    class Bridge(QObject):
        devices = Signal()
        state = Signal(str)
        stats = Signal(object)

    class Meter(QWidget):
        """Индикатор уровня L/R в дБFS с удержанием пика."""
        def __init__(self):
            super().__init__()
            self.level = [0.0, 0.0]
            self.hold = [0.0, 0.0]
            self.setMinimumHeight(34)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        @staticmethod
        def pos(v: float) -> float:
            if v <= 1e-5:
                return 0.0
            db = 20 * np.log10(v)
            return float(min(1.0, max(0.0, (db + 60) / 60)))

        def push(self, peak):
            for i in range(2):
                p = self.pos(float(peak[i]))
                self.level[i] = max(p, self.level[i] - 0.03)
                self.hold[i] = p if p >= self.hold[i] else max(0.0, self.hold[i] - 0.004)
            self.update()

        def paintEvent(self, _):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            bh = (h - 6) / 2
            for i, lab in enumerate("LR"):
                y = i * (bh + 6)
                p.setPen(QColor("#8b93a7"))
                p.drawText(0, int(y), 14, int(bh), Qt.AlignVCenter, lab)
                x0, bw = 18, w - 18
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#1d212b"))
                p.drawRoundedRect(x0, y, bw, bh, 4, 4)
                lv = self.level[i]
                col = "#22c55e" if lv < 0.85 else ("#f59e0b" if lv < 0.97 else "#ef4444")
                p.setBrush(QColor(col))
                if lv > 0:
                    p.drawRoundedRect(x0, y, bw * lv, bh, 4, 4)
                if self.hold[i] > 0:
                    p.setBrush(QColor("#e6e8ee"))
                    p.drawRect(int(x0 + bw * self.hold[i]) - 2, int(y), 2, int(bh))

    class Main(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("LanAudio — звук на телефон")
            self.resize(460, 720)
            self.cfg = QSettings("LanAudio", "sender")
            self.bridge = Bridge()
            self.found: dict[str, tuple[str, int, dict]] = {}
            self.sender: Sender | None = None

            root = QWidget(); self.setCentralWidget(root)
            v = QVBoxLayout(root); v.setContentsMargins(18, 16, 18, 16); v.setSpacing(6)
            t = QLabel("LanAudio"); t.setObjectName("title"); v.addWidget(t)
            hint = QLabel("Системный звук Windows → телефон по Wi-Fi, стерео 48 кГц"); hint.setObjectName("hint")
            v.addWidget(hint)

            v.addWidget(self._section("ТЕЛЕФОН"))
            self.list = QListWidget(); self.list.setMinimumHeight(110)
            self.list.currentItemChanged.connect(self.on_pick)
            self.list.itemDoubleClicked.connect(lambda *_: self.toggle() if not self.sender else None)
            v.addWidget(self.list)
            self.searching = QLabel("Поиск телефонов…"); self.searching.setObjectName("hint"); self.searching.setWordWrap(True)
            v.addWidget(self.searching)
            self.addr = QLineEdit(self.cfg.value("addr", "", str))
            self.addr.setPlaceholderText("или IP телефона вручную, например 192.168.100.3")
            v.addWidget(self.addr)

            v.addWidget(self._section("ИСТОЧНИК ЗВУКА"))
            row = QHBoxLayout()
            self.dev = QComboBox(); self.dev.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            row.addWidget(self.dev)
            ref = QPushButton("↻"); ref.setToolTip("Обновить список устройств"); ref.setFixedWidth(40)
            ref.clicked.connect(self.fill_devices)
            row.addWidget(ref)
            v.addLayout(row)

            self.surround = QCheckBox("Объёмный звук 3D (5.1/7.1 → наушники)")
            self.surround.setChecked(self.cfg.value("surround", True, bool))
            self.surround.setToolTip("Для PUBG и др.: направление шагов и выстрелов, спереди/сзади.\n"
                                     "Нужно, чтобы в Windows устройство было настроено на 5.1/7.1, а в игре — объёмный звук.")
            v.addWidget(self.surround)

            v.addWidget(self._section("КАЧЕСТВО"))
            self.codec = QComboBox()
            for label, *_ in CODECS:
                self.codec.addItem(label)
            self.codec.setCurrentIndex(self.cfg.value("codec", 0, int))
            v.addWidget(self.codec)
            self.frame = QComboBox()
            for label, _ in FRAMES:
                self.frame.addItem("Пакет " + label)
            self.frame.setCurrentIndex(self.cfg.value("frame", 1, int))
            v.addWidget(self.frame)

            v.addWidget(self._section("УРОВЕНЬ"))
            self.meter = Meter(); v.addWidget(self.meter)

            v.addSpacing(8)
            self.btn = QPushButton("Старт"); self.btn.setObjectName("primary")
            self.btn.clicked.connect(self.toggle)
            v.addWidget(self.btn)
            self.status = QLabel("Выберите телефон и нажмите «Старт»"); self.status.setObjectName("hint")
            self.status.setWordWrap(True)
            v.addWidget(self.status)
            self.stats = QLabel(""); self.stats.setObjectName("stats"); self.stats.setVisible(False)
            v.addWidget(self.stats)
            v.addStretch(1)

            self.bridge.devices.connect(self.refresh_devices)
            self.bridge.state.connect(self.status.setText)
            self.bridge.stats.connect(self.show_stats)
            self.fill_devices()

            self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(33)

            self.zc = Zeroconf()
            self.browser = ServiceBrowser(self.zc, SERVICE_TYPE, handlers=[self.on_service])
            QTimer.singleShot(6000, lambda: self.searching.setText(
                "" if self.found else "Не нашёл. Телефон в той же Wi-Fi сети и LanAudio на нём включён? "
                                      "Можно ввести IP вручную."))

        def _section(self, text):
            l = QLabel(text); l.setObjectName("section"); return l

        # ---------------- устройства
        def fill_devices(self):
            want = self.dev.currentData() or self.cfg.value("device", DEFAULT_DEVICE, str)
            self.dev.clear()
            self.dev.addItem("Системный звук по умолчанию", DEFAULT_DEVICE)
            try:
                import pyaudiowpatch as pyaudio
                pa = pyaudio.PyAudio()
                try:
                    for n in wasapi_outputs(pa):
                        self.dev.addItem(n, n)
                finally:
                    pa.terminate()
            except Exception as e:  # noqa: BLE001
                self.status.setText(f"WASAPI недоступен: {e}")
            i = self.dev.findData(want)
            self.dev.setCurrentIndex(max(0, i))

        # ---------------- mDNS
        def on_service(self, zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Removed:
                self.found.pop(name, None)
            else:
                info = zeroconf.get_service_info(service_type, name, 3000)
                if not info:
                    return
                addrs = info.parsed_addresses()
                ip4 = [a for a in addrs if ":" not in a]
                if not (ip4 or addrs):
                    return
                props = {k.decode(): (v or b"").decode() for k, v in (info.properties or {}).items()}
                self.found[name] = ((ip4 or addrs)[0], info.port, props)
            self.bridge.devices.emit()

        def refresh_devices(self):
            cur = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
            self.list.clear()
            for name, (ip, port, props) in sorted(self.found.items()):
                label = name.replace("." + SERVICE_TYPE, "")
                it = QListWidgetItem(f"{label}\n{ip}")
                it.setData(Qt.UserRole, name)
                self.list.addItem(it)
                if name == cur or (cur is None and ip == self.addr.text().split(":")[0]):
                    self.list.setCurrentItem(it)
            if self.found and not self.list.currentItem():
                self.list.setCurrentRow(0)
            self.searching.setText(f"Найдено: {len(self.found)}" if self.found else "Поиск телефонов…")

        def on_pick(self, item, _prev=None):
            if not item:
                return
            ip, port, _ = self.found.get(item.data(Qt.UserRole), (None, None, {}))
            if ip:
                self.addr.setText(ip if port == CTRL_PORT else f"{ip}:{port}")

        # ---------------- старт/стоп
        def toggle(self):
            if self.sender:
                s, self.sender = self.sender, None
                threading.Thread(target=s.stop, daemon=True).start()
                self.set_running(False)
                return
            text = self.addr.text().strip()
            if not text:
                self.status.setText("Не выбран телефон")
                return
            host, _, port = text.partition(":")
            _, codec, br = CODECS[self.codec.currentIndex()]
            fms = FRAMES[self.frame.currentIndex()][1]
            self.cfg.setValue("addr", text)
            self.cfg.setValue("codec", self.codec.currentIndex())
            self.cfg.setValue("frame", self.frame.currentIndex())
            self.cfg.setValue("device", self.dev.currentData())
            self.cfg.setValue("surround", self.surround.isChecked())
            self.sender = Sender(host, int(port or CTRL_PORT), device=self.dev.currentData(), codec=codec,
                                 surround=self.surround.isChecked(),
                                 frame_ms=fms, bitrate=br or 256000,
                                 on_state=self.bridge.state.emit, on_stats=self.bridge.stats.emit)
            self.sender.start()
            self.set_running(True)

        def set_running(self, on: bool):
            self.btn.setText("Стоп" if on else "Старт")
            self.btn.setObjectName("danger" if on else "primary")
            self.btn.style().unpolish(self.btn); self.btn.style().polish(self.btn)
            for w in (self.list, self.addr, self.dev, self.codec, self.frame, self.surround):
                w.setEnabled(not on)
            if not on:
                self.stats.setVisible(False)

        def tick(self):
            s = self.sender
            if s is not None:
                pk = s.peak.copy(); s.peak[:] = 0
                self.meter.push(pk)
            else:
                self.meter.push((0.0, 0.0))

        def show_stats(self, d):
            if not d or not self.sender:
                self.stats.setVisible(False)
                return
            fms = self.sender.frame_ms
            phone = d.get("buffer_ms", 0) + d.get("output_ms", 0)
            # ~10 мс — период движка WASAPI (loopback отдаёт звук блоками по 10 мс) + 1 пакет + ~2 мс Wi-Fi
            total = 10 + fms + 2 + phone
            ll = "low-latency" if d.get("low_latency") else "ОБЫЧНЫЙ вывод (наушники BT?)"
            self.stats.setText(
                f"Задержка ≈ {total:.0f} мс (оценка сквозная)\n"
                f"Телефон: буфер {d.get('buffer_ms', 0):.1f} мс (цель {d.get('target_ms', 0):.1f}), "
                f"вывод {d.get('output_ms', 0):.1f} мс\n"
                f"Джиттер {d.get('jitter_ms', 0):.1f} мс · {ll}\n"
                f"Потери {d.get('lost', 0)} · опоздали {d.get('late', 0)} · "
                f"срывы {d.get('underruns', 0)} · сбросы {d.get('dropped', 0)}"
                + ("" if d.get("playing") else "\nНа ПК тишина — телефон ждёт звук"))
            self.stats.setVisible(True)

        def closeEvent(self, e):
            if self.sender:
                self.sender.stop()
            try:
                self.zc.close()
            except Exception:  # noqa: BLE001
                pass
            super().closeEvent(e)

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = Main(); w.show()
    return app.exec()


def boost_process():
    """Выше приоритет и таймер 1 мс: GUI и сборщик мусора не задерживают аудио-поток."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x8000)  # ABOVE_NORMAL
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    boost_process()
    ap = argparse.ArgumentParser(description="LanAudio — звук Windows → Android")
    ap.add_argument("--list", action="store_true", help="показать устройства вывода")
    ap.add_argument("--headless", metavar="HOST", help="без GUI: передавать на телефон HOST")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--codec", choices=["pcm", "opus"], default="pcm")
    ap.add_argument("--frame-ms", type=float, default=5.0)
    ap.add_argument("--source", choices=["loopback", "silence", "tone"], default="loopback")
    ap.add_argument("--device", default=None)
    ap.add_argument("--surround", action="store_true", help="5.1/7.1 → объёмное бинауральное стерео")
    a = ap.parse_args()
    if a.list:
        cmd_list(); return 0
    if a.headless:
        return cmd_headless(a.headless, a.seconds, a.codec, a.frame_ms, a.source, a.device, a.surround)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
