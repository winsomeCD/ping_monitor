# Ping Check Enhanced — Network Monitor

Multi-host ICMP reachability & latency dashboard built with **pure PySide6** (no Fluent Widgets or other UI frameworks).

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-green)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey)

---

## Features

- **Multi-host monitoring** — ping many hosts/IPs at once with individual worker threads
- **Live dashboard** — status, sequence, TTL, latency, packet loss, consecutive failures
- **Latency history chart** — rolling-window graph with per-host colours and legend
- **Down-hosts panel** — quick view of currently unreachable targets
- **Host detail dialog** — double-click a row for stats + recent timeline
- **Theme toggle** — dark / light mode (🌙 / ☀️)
- **Import hosts** — load from `.txt` or `.csv`
- **Config save/load** — persists hosts and settings to `ping_monitor_config.json`
- **Export reports** — TXT summary or CSV (summary + probe log)
- **Search / filter** — live filter on the host table
- **Log limiting** — timeline capped at 1000 entries per host

---

## Requirements

```bash
pip install PySide6
```

Only **PySide6** is required. No additional UI libraries.

### ICMP permissions (Linux)

The app uses the non-privileged ICMP socket (`SOCK_DGRAM` + `IPPROTO_ICMP`).  
Ensure your user is allowed:

```bash
# temporary
sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"

# permanent — add to /etc/sysctl.conf or a drop-in
net.ipv4.ping_group_range = 0 2147483647
```

> **Windows / macOS:** The current ICMP worker is Linux-specific. On other platforms replace `ping_worker` with `icmplib` or a `subprocess` call to the system `ping` command.

---

## Quick start

```bash
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
| **Live Host Dashboard** | Main table (Host, Status, Sequence, TTL, Latency, Loss, Fails) |
| **Latency History** | Custom-painted rolling chart |
| **Down Hosts** | Compact table of currently down targets |
| **Footer** | Aggregate host count and overall loss |

---

## Config file

On first **Save Config**, settings are written to:

```
ping_monitor_config.json
```

Fields:

```json
{
  "hosts": "8.8.8.8, 1.1.1.1, google.com",
  "packet_size": 56,
  "interval": 1.0,
  "timeout": 1.0,
  "history_points": 60
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
- Summary rows + Down rows
- Probe-level log (sequence, timestamp, result, latency)

---

## Project layout

```
.
├── ping_monitor.py   # Full application (pure PySide6)
└── README.md
```

---

## Notes & limitations

- ICMP implementation relies on Linux `recvmsg` ancillary data for TTL.
- Chart history is limited by the “Chart History” spin box (default 60 probes).
- Timeline / log rows are capped at 1000 entries per host to bound memory.
- Theme is applied via a single large stylesheet; toggle rebuilds styles and repaints the chart.

---

## License

Use and modify freely for personal or internal tooling.
