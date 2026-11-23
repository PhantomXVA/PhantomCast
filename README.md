# LanAudio

**Stream your Windows system audio to an Android phone over Wi-Fi — in real time, with ~30–50 ms latency.**

Use your phone as a wireless speaker / headphone output for your PC: games, videos, music, calls.
No cloud, no accounts, no drivers — just your local network.

🇷🇺 [Читать на русском](README.ru.md)

[![Android build](../../actions/workflows/android.yml/badge.svg)](../../actions/workflows/android.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/PC-Windows%2010%2F11-blue)
![Platform](https://img.shields.io/badge/phone-Android%208%2B-brightgreen)

## Features

- 🎧 **Low latency** — 5 ms packets, adaptive jitter buffer, `AudioTrack` low-latency mode. ≈ 30–50 ms end to end on 5 GHz Wi-Fi.
- 🔊 **Lossless or compressed** — PCM 16-bit (1.5 Mbit/s) or Opus 128 / 192 / 320 kbit/s (`lowdelay`).
- 🔎 **Zero-config discovery** — the phone announces itself over mDNS; the PC lists it, click *Start*. Manual IP is available too.
- 🔌 **Auto-reconnect** — survives Wi-Fi hiccups and device changes.
- 🔋 **Battery friendly** — while no PC is connected the phone only listens on a port; audio, WakeLock and WifiLock are on only during a session. Screen can be off.
- 📊 **Live stats** — latency estimate, jitter, packet loss, underruns on both sides; L/R level meter on PC.
- 🪶 **Tiny** — Android app has no AndroidX/third-party dependencies (~0.9 MB APK).
- 🛡️ **Firewall-friendly** — the PC only makes outgoing connections; nothing to allow in Windows Firewall.

## Quick start

1. **Phone:** download `LanAudio-debug.apk` from [Releases](../../releases), install it (allow unknown sources), open **LanAudio**. It starts listening and shows *"Waiting for PC"*.
2. **PC:** install Python 3.10+, then run `windows\run.bat`. The first run creates a virtualenv and installs dependencies.
3. Pick your phone in the list (or type its IP) and press **Start**. Done.

> Phone and PC must be on the same Wi-Fi network (not a guest network with client isolation).
> On Samsung: *Settings → Battery → Background limits → LanAudio → Unrestricted*.

## How it works

```
Windows                                       Android
┌──────────────────────┐   TCP 50510 (JSON)   ┌──────────────────────┐
│ WASAPI loopback      │ ───────────────────▶ │ control server       │
│  → resample / mixdown│ ◀─────────────────── │ stats every 0.5 s    │
│  → PCM or Opus (5 ms)│                      │                      │
│  → UDP packets       │   UDP 50511 (audio)  │ jitter buffer        │
│                      │ ───────────────────▶ │  → AudioTrack (LL)   │
└──────────────────────┘   mDNS _lanaudio._tcp└──────────────────────┘
```

- **The phone is the server**, the PC connects to it. Discovery is trivial, and Windows never asks for firewall permissions.
- **Audio over UDP** — one packet = 2.5 / 5 / 10 ms of sound. On Wi-Fi it's better to lose 5 ms than to stall the stream waiting for a retransmit (TCP).
- **Control over TCP** — JSON lines: hello, stats, ping. If TCP drops, the session ends and sound stops immediately.
- **Jitter buffer** keeps the minimum fill level of the last second near a small target, speeds up/slows down playback by up to 0.3 % (inaudible) to absorb clock drift, and drops stale audio after a Wi-Fi burst. On underrun the margin grows by 4 ms and then decays back.

Full details of the protocol are in the header of [`Proto.kt`](android/app/src/main/java/dev/lanaudio/Proto.kt).

### Latency budget

| Stage | Approx. |
|---|---|
| WASAPI loopback block | 10 ms |
| Packet (default 5 ms) | 5 ms |
| Wi-Fi | 1–5 ms |
| Jitter buffer | 2–10 ms |
| Phone output (AudioTrack + HAL + DAC) | 10–20 ms |
| **Total** | **≈ 30–50 ms** |

Bluetooth headphones on the phone add their own 100–250 ms — that's the BT codec, not LanAudio.

## Build from source

**Windows sender** (Python 3.10+):

```powershell
cd windows
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python lanaudio_sender.py            # GUI
.venv\Scripts\python lanaudio_sender.py --list     # list output devices
.venv\Scripts\python lanaudio_sender.py --headless 192.168.1.50 --seconds 10 --codec opus --source tone
```

**Android receiver** (JDK 17, Android SDK 34; Gradle 8.7 won't run on JDK 25):

```powershell
cd android
.\gradlew.bat assembleDebug          # or build-apk.ps1 (uses Android Studio's JDK)
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Stack: Kotlin 1.9, AGP 8.5, minSdk 26 · Python, PySide6, PyAudioWPatch, soxr, PyAV (libopus), zeroconf.

## Ports

| Purpose | Port | Where |
|---|---|---|
| Control | TCP 50510 | phone |
| Audio | UDP 50511 | phone |
| Discovery | mDNS `_lanaudio._tcp` | phone |

## Troubleshooting

- **Phone not found** — same network? No client isolation? Type its IP manually.
- **Crackling / dropouts** — use 10 ms packets, raise *Minimum margin* to 10–20 ms, prefer 5 GHz Wi-Fi and a wired PC; disable Wi-Fi power saving on the PC adapter.
- **Connection drops** — exclude LanAudio from battery optimisation on the phone (see above).
- **Sound also plays on PC speakers** — loopback is a copy. Route an app to an unused output device (*Settings → Sound → App volume*) and choose that device as the source.
- **Silence on PC** — WASAPI loopback only delivers data while something is playing; the phone shows *"Connected · silence"*.

## Roadmap

- [ ] Signed release APK and installer for the PC app
- [ ] System tray mode / start with Windows
- [ ] Linux / macOS sender
- [ ] Multiple phones at once
- [ ] Per-application capture

## Contributing

Issues and pull requests are welcome. Please describe your Wi-Fi setup and attach the stats line from the PC window when reporting audio problems.

## License

[MIT](LICENSE) © PhantomXVA
