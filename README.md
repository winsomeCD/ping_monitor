# Ping Check Enhanced — Network Monitor

Multi-host ICMP reachability and latency dashboard built with **PySide6** and the cross-platform **icmplib** probe backend.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

---

## Features

- **Multi-host monitoring** — no fixed target-count or probe-concurrency cap; all targets use async probes
- **Live dashboard** — status, sequence, latency, packet loss, consecutive failures
- **Latency history chart** — rolling-window graph with per-host colours and legend
- **Down-hosts panel** — quick view of currently unreachable targets
- **Sortable dashboards** — live and degraded/down tables sort by column, with numeric fields ordered numerically
- **Host detail dialog** — double-click a row for stats + recent timeline
- **Theme toggle** — dark / light mode (🌙 / ☀️)
- **Import hosts** — load from `.txt` or `.csv`
- **Config save/load** — atomically persists hosts, settings, and theme to `ping_monitor_config.json`
- **Export reports** — TXT summary or CSV with summary, down-host, and per-probe rows
- **Search / filter** — live filter on the host table
- **Log limiting** — timeline capped at 1000 entries per host

---

## Requirements

```bash
python -m pip install -r requirements.txt
```

This installs **PySide6** for the desktop UI and **icmplib** for portable ICMP probes.

### ICMP permissions

The probe backend uses unprivileged ICMP sockets where the operating system supports them. If a platform or local policy denies ICMP access, the affected target reports **Permission Denied** with the underlying error available in its status tooltip.

```bash
# Linux systems may need to allow unprivileged ping sockets:
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
```

---

## Quick start

```bash
python -m pip install -r requirements.txt
python ping_monitor.py
```

1. Enter hosts (comma-separated) or click **Import**.
2. Adjust packet size, interval, timeout, and chart history if needed.
3. Click **Start Monitoring**.
4. Double-click a row for details; use **Export TXT / CSV** when finished.

---

## UI overview

| Area | Description |
|------|-------------|
| **Header** | Title, theme toggle, status pill (IDLE / MONITORING) |
| **Config card** | Host list, import, save config, spin boxes, Start/Stop, Clear, Export |
| **Filter** | Live search over host names/IPs |
| **Live Host Dashboard** | Main table (Host, Status, Sequence, Latency, Loss, Fails) |
| **Latency History** | Custom-painted rolling chart |
| **Down Hosts** | Compact table of currently down targets |
| **Footer** | Aggregate host count and overall loss |

---

## Config file

On first **Save Config**, settings are written to:

```
ping_monitor_config.json
```

The path is anchored to the application directory, so launching from another working directory uses the same settings file. Writes are atomic. Invalid config is reported and the application continues with defaults; unsupported values are rejected without partially applying the file.

Fields:

```json
{
  "hosts": "8.8.8.8, 1.1.1.1, google.com",
  "packet_size": 56,
  "interval": 1.0,
  "timeout": 1.0,
  "history_points": 60,
  "dark_theme": true
}
```

Loaded automatically on startup if present.

---

## Export formats

### TXT report
- Aggregated metrics (sent / recv / loss / min / max / avg)
- Hosts down at export time
- Full sequence timeline per host

### CSV report
- Summary metrics per target
- Current down/error state per target
- Recent probe rows (sequence, timestamp, result, latency or error)

---

## Project layout

```
.
├── README.md
├── ping_monitor.py          # PySide6 application and dashboard
├── probes.py                # Cross-platform async ICMP backend and worker task
├── requirements.txt
└── tests/
    ├── test_ping_monitor.py
    └── test_probes.py
```

Run the regression tests with:

```bash
python -m unittest discover -s tests
```

---

## Notes & limitations

- The portable probe backend does not expose reply TTL, so the dashboard focuses on latency and reachability.
- Chart history is bounded by the “Chart History” spin box (default 60 probes); aggregate min/max/average use the full run.
- Timeline / log rows are capped at 1000 entries per host to bound memory.
- Stop requests finish any probe already in flight before enabling a new run or report export.
- Every target is probed concurrently with asyncio; a single background event-loop thread avoids creating one OS thread per target. Target count is constrained only by available memory, OS socket limits, and UI performance.

---

## License

Use and modify freely for personal or internal tooling. This project depends on icmplib (LGPLv3); follow its license terms when redistributing the application.
