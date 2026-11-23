# Changelog

## 0.1.1
- Sender: no longer drops the session on short Wi-Fi stalls (control read timeout removed, 15 s silence watchdog, TCP keepalive).
- Sender: reconnect delay 2 s -> 0.5 s; above-normal process priority and 1 ms timer resolution on Windows.
- Receiver: control timeout 6 s -> 20 s; UDP receive loop survives transient errors.

## 0.1.0
- First release: WASAPI loopback -> UDP -> Android AudioTrack (low latency), PCM / Opus, mDNS discovery.