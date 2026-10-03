# Ping Check Enhanced — Network Monitor

Multi-host ICMP reachability and latency dashboard built with **PySide6**. Features high-performance multiplexed asynchronous raw ICMP probing, TTL-cached DNS resolution, and Linux **fping** support when installed.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

---

## Operating System Compatibility

The monitor runs cross-platform on **Windows**, **Linux**, and **macOS**, automatically configuring the optimal probe engine for each environment:

### 🪟 Windows OS
* **Engine**: Runs the high-performance **`AsyncICMPSession`** multiplexed raw socket engine.
* **Concurrency**: Seamlessly monitors 300 to 1,000+ parallel targets with an enlarged 1MB socket buffer (`SO_RCVBUF`), eliminating the Windows raw socket broadcast bottleneck.
* **Permissions**: Works out of the box with standard user or Administrator privileges.
* **Command**:
  ```powershell
  python .\ping_monitor.py
  ```

### 🐧 Linux OS
* **Dual-Engine Architecture**:
  1. **Native `fping` Mode (Primary)**: When `fping` (v5.5+ with `--json` support) is installed, the monitor automatically uses fping's native single-process paced event loop.
     ```bash
     # Debian / Ubuntu / Mint:
     sudo apt install fping
     # Fedora / RHEL:
     sudo dnf install fping
     # Arch Linux:
     sudo pacman -S fping
     ```
  2. **Multiplexed Python Socket Mode (Fallback)**: If `fping` is not installed or lacks JSON support, the application automatically uses the cross-platform `AsyncICMPSession` / `icmplib` engine.
* **Permissions**: To run without `sudo`/root, enable unprivileged ping sockets:
  ```bash
  sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
  ```
* **Command**:
  ```bash
  python3 ping_monitor.py
  ```

### 🍎 macOS
* **Engine**: Uses PySide6 and the cross-platform asynchronous socket backend.
* **Command**:
  ```bash
  python3 ping_monitor.py
  ```

### Platform Compatibility Matrix

| Feature | Windows | Linux | macOS |
|---|---|---|---|
| **PySide6 Desktop GUI** | Supported | Supported (X11 & Wayland) | Supported |
| **300–1000 Parallel Probes** | Supported (`AsyncICMPSession`) | Supported (`fping` / `AsyncICMPSession`) | Supported (`AsyncICMPSession`) |
| **DNS TTL Caching & Pacing** | Supported (`DNSResolver`) | Supported (`DNSResolver`) | Supported (`DNSResolver`) |
| **TXT / CSV / XLSX Export** | Supported | Supported | Supported |
| **Dark / Light Theme** | Supported | Supported | Supported |

---

## Features

- **High-concurrency monitoring** — easily monitors 300 to 1,000+ parallel targets with multiplexed asynchronous raw sockets (`AsyncICMPSession`) and zero socket-buffer duplication.
- **Smart DNS caching & pacing** — resolves hostnames once, caches for 5 minutes (`DNS_CACHE_TTL = 300s`), and paces initial queries via semaphore to prevent DNS server/threadpool flooding.
- **Live dashboard** — status, sequence, latency, packet loss, consecutive failures.
- **Latency history chart** — rolling-window graph with per-host colours and legend.
- **No-reply panel** — shows targets that did not answer ICMP; a timeout does not prove the host is offline.
- **Sortable dashboards** — live and no-reply tables sort by column, with numeric fields ordered numerically.
- **Host detail dialog** — double-click a row for stats + recent timeline.
- **Theme toggle** — dark / light mode (🌙 / ☀️).
- **Import hosts** — load from `.txt` or `.csv` (supports 500+ host lists).
- **Config save/load** — atomically persists hosts, settings, and theme to `ping_monitor_config.json`.
- **Export reports** — TXT with a top-level overview, CSV with per-host summary and probe rows, or XLSX with Summary Info, Host Details, and Probe Details tabs.
- **Search / filter** — live filter on the host table.
- **Log limiting** — timeline capped at 1000 entries per host.

---

## Probing Architecture & High-Concurrency Engine

### 1. Multiplexed Raw Socket Engine (`AsyncICMPSession`)
- **Single Socket Multiplexing**: Rather than opening hundreds of separate raw sockets (which causes Windows to clone every incoming ICMP packet across every open socket, resulting in $N \times N$ packet floods), the monitor utilizes a unified IPv4/IPv6 socket pair with an enlarged 1MB buffer (`SO_RCVBUF`).
- **$O(1)$ Asynchronous Dispatch**: A background listener receives incoming ICMP replies and dispatches them directly to the matching host's `asyncio.Future` by `(id, sequence)`.
- **High Concurrency Stability**: Reliably handles 300–1000 parallel hosts without packet buffer overflows or false timeout drops.

### 2. DNS Caching & Resolver (`DNSResolver`)
- **TTL Caching**: Caches resolved IP addresses for 5 minutes (`DNS_CACHE_TTL = 300s`). Periodic ping probes fire directly to the resolved IP address, eliminating repetitive DNS lookups on every 1-second tick.
- **Paced DNS Semaphore**: Paces initial hostname lookups (`MAX_CONCURRENT_DNS_LOOKUPS = 40`) to protect the local DNS cache and prevent asyncio thread pool starvation.
- **Stale-While-Revalidate Fallback**: If a background DNS refresh experiences a temporary packet drop, the engine reuses the last known valid IP rather than reporting a false `DNS Error`.
- **Negative Caching**: Caches non-existent domains for 10 seconds to avoid hammering DNS servers with known failed lookups.

### 3. Dual-Engine Support
- **Linux**: Automatically detects and uses `fping` (with `--json` support) when installed for native pacing.
- **Windows / Fallback**: Uses the high-performance `AsyncICMPSession` multiplexed raw engine, with automatic fallback to portable `icmplib.async_ping`.

---

## Requirements

```bash
python -m pip install -r requirements.txt
```

This installs **PySide6** for the desktop UI, **icmplib** for portable ICMP socket operations, and **openpyxl** for Excel workbook export.

---

## Quick Start

```bash
python -m pip install -r requirements.txt
python ping_monitor.py
```

1. Enter hosts (comma-separated) or click **Import** (e.g. `ping_domains.txt`).
2. Adjust packet size, interval, timeout, chart history, and maximum parallel probes (up to 1000).
3. Click **Start Monitoring**.
4. Double-click a row for details; use **Export TXT / CSV / XLSX** when finished.

---

## UI Overview

| Area | Description |
|------|-------------|
| **Header** | Title, theme toggle, status pill (IDLE / MONITORING) |
| **Config card** | Host list, import, save config, spin boxes, Start/Stop, Clear, Export |
| **Filter** | Live search over host names/IPs |
| **Live Host Dashboard** | Main table (Host, Status, Sequence, Latency, Loss, Fails) |
| **Latency History** | Custom-painted rolling chart |
| **No ICMP Reply** | Targets that returned no echo reply before timeout |
| **Footer** | Aggregate host count and overall loss |

---

## Config File

On first **Save Config**, settings are written to:

```
ping_monitor_config.json
```

The path is anchored to the application directory. Writes are atomic. Invalid config is reported and the application continues with defaults.

Fields:

```json
{
  "hosts": "8.8.8.8, 1.1.1.1, google.com",
  "packet_size": 56,
  "interval": 1.0,
  "timeout": 2.0,
  "max_concurrent": 1000,
  "history_points": 60,
  "dark_theme": true
}
```

---

## Export Formats

Export dialogs now automatically generate timestamped default filenames (`network_report_YYYYMMDD_HHMMSS.<ext>`) and embed the export generation timestamp across all report types:

### TXT Report (`network_report_YYYYMMDD_HHMMSS.txt`)
- Header includes `Generated: YYYY-MM-DD HH:MM:SS`
- Overview counts and aggregate ICMP/latency metrics at the top
- Aggregated metrics (sent / recv / loss / min / max / avg)
- Targets with no ICMP reply at export time (with failure timestamps)
- Full sequence timeline per host

### CSV Report (`network_report_YYYYMMDD_HHMMSS.csv`)
- Embedded `Timestamp` and `Export_DateTime` fields on all rows
- Summary metrics per target
- No-reply and per-probe rows
- Current ICMP reply and probe error state per target
- Recent probe rows (sequence, timestamp, result, latency or error)

### Excel Workbook (`network_report_YYYYMMDD_HHMMSS.xlsx`)
- **Summary Info**: Overview tab with `Report Generated` timestamp
- **Host Details**: Metrics, summary per target, and `Report Generated` timestamp column
- **Probe Details**: Recent probe timeline with sequence timestamps and `Report Generated` column

---

## Project Layout

```
.
├── README.md                # Project documentation and architecture guide
├── worklog.md               # Detailed technical analysis and changelog
├── ping_monitor.py          # PySide6 application and dashboard UI
├── probes.py                # Multiplexed ICMP session & DNS caching engine
├── ping_domains.txt         # 500-host sample import file
├── IP-test.txt              # Sample IP target list
├── requirements.txt         # Python dependencies
└── tests/
    ├── test_ping_monitor.py # UI and config tests
    └── test_probes.py       # Probe, multiplexing, and DNS tests
```

Run the regression tests with:

```bash
python -m unittest discover tests
```

---

## Notes & Limitations

- Many public websites block or rate-limit ICMP while their web service remains available. “No ICMP Reply” means only that no echo reply arrived before timeout.
- DNS, permission, and probe setup errors are shown separately and are excluded from ICMP packet-loss totals because no echo request was sent.
- The default ICMP timeout is 2 seconds; longer paths or congested networks may need a higher value.
- Chart history is bounded by the “Chart History” spin box (default 60 probes); aggregate min/max/average use the full run.
- Timeline / log rows are capped at 1000 entries per host to bound memory.
- Stop requests finish any probe already in flight before enabling a new run or report export.
