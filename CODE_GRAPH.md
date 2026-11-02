# Code Graph — ping_monitor

> Generated via `code-graph` skill. Cross-platform ICMP/TCP/HTTP latency telemetry application architecture and PyQt5 component graph.

---

## 1. System Architecture & Component Topology

```mermaid
flowchart TD
    subgraph App Controller & Config
        MAIN["ping_monitor.py (PingMonitorApp: QMainWindow)"]
        CONF["ping_monitor_config.json"]
        PROBES["probes.py (ICMP, TCP, HTTP, DNS Probing Engines)"]
    end

    subgraph User Interface Layout
        SIDE["Sidebar Console (Vitals HUD, Hosts, Timing)"]
        TABLES["Telemetry Matrix & Unreachable Tables"]
        CHART["LatencyChart (High-DPI Waveform Renderer)"]
        DIALOGS["HostDetailDialog & DNSIssuesDialog"]
    end

    subgraph Concurrency & Threads
        POOL["QThreadPool / Worker Threads"]
    end

    MAIN --> CONF
    MAIN --> PROBES
    MAIN --> SIDE & TABLES & CHART & DIALOGS
    SIDE -->|Triggers Start| POOL
    POOL --> PROBES
    PROBES -->>|PyQt Signals| MAIN
    MAIN -->|Batch UI Updates| TABLES & CHART & SIDE
```

---

## 2. Symbol & Method Dictionary

| Class / Function | Module | Description |
| :--- | :--- | :--- |
| `PingMonitorApp` | `ping_monitor.py` | Main controller managing probes, configuration, styling, and exports |
| `LatencyChart` | `ping_monitor.py` | Anti-aliased canvas rendering multi-host latency history curves |
| `HostDetailDialog` | `ping_monitor.py` | Deep statistics inspection modal with interactive target timeline |
| `DNSIssuesDialog` | `ping_monitor.py` | Shows DNS resolution bottlenecks and suggested public nameservers |
| `PingWorker` | `probes.py` | Multi-threaded runner executing ICMP, TCP, and HTTP probe requests |
| `batch_ping()` | `probes.py` | Parallel ping execution supporting Windows and POSIX environments |

---

## 3. Test Coverage & Verification

- `tests/test_ping_monitor.py`: Comprehensive test suite for UI setup, configuration loading, theme switching, and table population (24 tests).
- `tests/test_probes.py`: Verification of ICMP raw ping, TCP socket connection, HTTP status probing, and DNS fallbacks (20 tests).
- Total: **44 unit tests passing**.
