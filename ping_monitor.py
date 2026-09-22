#!/usr/bin/env python3
"""
Ping Check Enhanced - Multi-Host Network Monitor (Pure PySide6)

Features:
- Theme toggle (Light/Dark)
- Export snapshot (TXT/CSV), log limiting, config save/load, search, detail dialog
- Live latency chart, down-hosts panel, threaded ICMP probes

Install:
    pip install PySide6

Note: The ICMP socket used is Linux-specific. On Windows, replace ping_worker
with icmplib or subprocess ping.
"""

import sys
import os
import csv
import re
import socket
import struct
import threading
import time
import json

# --- Environment setup ---
os.environ.setdefault("QT_NO_GLIB", "1")
os.environ.setdefault("GSETTINGS_BACKEND", "memory")
os.environ.setdefault("GIO_USE_VFS", "local")
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.qpa.*=false;qt.gui.imageio.*=false;*.warning=false",
)
for _proxy_var in (
    "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
    "SOCKS_PROXY", "socks_proxy",
):
    # This monitor uses raw ICMP and does not need HTTP/SOCKS proxy
    # configuration. Invalid desktop proxy values can make GLib print
    # repeated "The given address is empty" messages at startup.
    os.environ.pop(_proxy_var, None)


class _FilteredStderr:
    _DROP = (
        "Error creating proxy: The given address is empty",
        "failed to commit changes to dconf: The given address is empty",
        "g-io-error-quark",
    )

    def __init__(self, real):
        self._real = real

    def write(self, msg):
        if msg and any(s in msg for s in self._DROP):
            return
        self._real.write(msg)

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def isatty(self):
        return self._real.isatty()


sys.stderr = _FilteredStderr(sys.stderr)

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QPalette, QIcon
from PySide6.QtNetwork import QNetworkProxyFactory
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QGridLayout, QSplitter, QFileDialog, QAbstractItemView, QHeaderView,
    QTableWidget, QTableWidgetItem, QLineEdit, QDialog, QTextEdit,
    QPushButton, QSpinBox, QDoubleSpinBox, QFrame, QMessageBox,
    QScrollArea, QSizePolicy, QAbstractSpinBox,
)

# ---------------------------------------------------------------------------
# ICMP helpers
# ---------------------------------------------------------------------------
def _icmp_checksum(data: bytes) -> int:
    total = 0
    count_to = (len(data) // 2) * 2
    count = 0
    while count < count_to:
        total += data[count + 1] * 256 + data[count]
        total &= 0xFFFFFFFF
        count += 2
    if count_to < len(data):
        total += data[-1]
        total &= 0xFFFFFFFF
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    answer = ~total & 0xFFFF
    answer = (answer >> 8) | ((answer << 8) & 0xFF00)
    return answer


def _build_icmp_echo_packet(icmp_id: int, seq: int, payload_size: int) -> bytes:
    header = struct.pack("!BBHHH", 8, 0, 0, icmp_id, seq)
    payload = bytes((0x08 + i) & 0xFF for i in range(payload_size))
    chksum = _icmp_checksum(header + payload)
    header = struct.pack("!BBHHH", 8, 0, chksum, icmp_id, seq)
    return header + payload


# ---------------------------------------------------------------------------
# Host Detail Dialog
# ---------------------------------------------------------------------------
class HostDetailDialog(QDialog):
    def __init__(self, host, data, dark=True, parent=None):
        super().__init__(parent)
        self.host = host
        self.data = data
        self._dark = dark
        self.setWindowTitle(f"Host Details - {host}")
        self.resize(600, 520)
        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QLabel(f"📊  {self.host}")
        header.setObjectName("detailTitle")
        layout.addWidget(header)

        stats_layout = QGridLayout()
        stats_layout.setSpacing(10)
        sent = self.data.get("sent", 0)
        received = self.data.get("received", 0)
        loss_pct = round(((sent - received) / sent) * 100, 1) if sent > 0 else 0.0
        valid_latencies = [l for l in self.data.get("latencies", []) if l > 0]
        stats = [
            ("Sent", sent),
            ("Received", received),
            ("Loss %", f"{loss_pct}%"),
            ("Samples", len(valid_latencies)),
        ]
        if valid_latencies:
            stats.extend([
                ("Min (ms)", f"{min(valid_latencies):.2f}"),
                ("Max (ms)", f"{max(valid_latencies):.2f}"),
                ("Avg (ms)", f"{sum(valid_latencies) / len(valid_latencies):.2f}"),
            ])
        for i, (label, value) in enumerate(stats):
            row, col = i // 2, (i % 2) * 2
            lbl = QLabel(f"{label}:")
            lbl.setObjectName("mutedLabel")
            val = QLabel(str(value))
            val.setObjectName("strongLabel")
            stats_layout.addWidget(lbl, row, col)
            stats_layout.addWidget(val, row, col + 1)
        layout.addLayout(stats_layout)

        status = self.data.get("last_status", "Unknown")
        status_color = "#68d391" if status == "Connected" else "#fc8181"
        status_row = QHBoxLayout()
        status_label = QLabel("Current Status: ")
        status_label.setObjectName("mutedLabel")
        status_value = QLabel(status)
        status_value.setStyleSheet(f"color: {status_color}; font-weight: 700;")
        status_row.addWidget(status_label)
        status_row.addWidget(status_value)
        status_row.addStretch()
        layout.addLayout(status_row)

        if self.data.get("last_down_time") and self.data.get("last_down_time") != "-":
            down_lbl = QLabel(f"Last Down: {self.data['last_down_time']}")
            down_lbl.setObjectName("mutedLabel")
            layout.addWidget(down_lbl)

        timeline_title = QLabel("Recent Timeline (last 50):")
        timeline_title.setObjectName("strongLabel")
        layout.addWidget(timeline_title)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(220)
        logs = self.data.get("timeline_stats", [])
        self.log_text.setText("\n".join(logs[-50:]) if logs else "No logs available.")
        layout.addWidget(self.log_text)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("primaryBtn")
        close_btn.setFixedHeight(36)
        close_btn.setMinimumWidth(120)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignCenter)

    def _apply_style(self):
        dark = self._dark
        bg = "#111925" if dark else "#ffffff"
        text = "#e6edf5" if dark else "#111827"
        muted = "#8b9cb3" if dark else "#4b5563"
        input_bg = "#0c131e" if dark else "#f9fafb"
        border = "#29364a" if dark else "#d1d5db"
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg};
                color: {text};
            }}
            QLabel#detailTitle {{
                font-size: 20px;
                font-weight: 700;
                color: {text};
                background: transparent;
            }}
            QLabel#mutedLabel {{
                color: {muted};
                background: transparent;
            }}
            QLabel#strongLabel {{
                color: {text};
                font-weight: 700;
                background: transparent;
            }}
            QTextEdit {{
                background-color: {input_bg};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 8px;
                font-family: Consolas, monospace;
                font-size: 12px;
            }}
            QPushButton#primaryBtn {{
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 8px;
                font-weight: 600;
                padding: 6px 18px;
            }}
            QPushButton#primaryBtn:hover {{
                background-color: #106ebe;
            }}
            QPushButton#primaryBtn:pressed {{
                background-color: #005a9e;
            }}
        """)


# ---------------------------------------------------------------------------
# Latency Chart
# ---------------------------------------------------------------------------
class LatencyChart(QWidget):
    COLORS_DARK = [
        "#63b3ed", "#68d391", "#f6ad55", "#fc8181",
        "#b794f4", "#4fd1c5", "#f687b3", "#a0aec0",
        "#90cdf4", "#9ae6b4", "#fbd38d", "#feb2b2",
        "#d6bcfa", "#81e6d9", "#fbb6ce", "#cbd5e0",
        "#4299e1", "#48bb78", "#ed8936", "#f56565",
        "#9f7aea", "#38b2ac", "#ed64a6", "#718096",
    ]
    COLORS_LIGHT = [
        "#3182ce", "#38a169", "#dd6b20", "#e53e3e",
        "#805ad5", "#319795", "#d53f8c", "#718096",
        "#2b6cb0", "#2f855a", "#c05621", "#c53030",
        "#6b46c1", "#2c7a7b", "#b83280", "#4a5568",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = {}
        self.max_points = 60
        self._dark = True
        self.setMinimumHeight(260)

    def set_dark(self, dark: bool):
        self._dark = dark
        self.update()

    def set_data(self, host_data, max_points):
        self.max_points = max(1, int(max_points))
        self.history = {
            host: list(data["latencies"][-self.max_points:])
            for host, data in host_data.items()
            if data.get("latencies")
        }
        self.update()

    @staticmethod
    def _elide(text, max_chars=16):
        text = str(text)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = self._dark
        bg = QColor("#1a1a1a" if dark else "#fafafa")
        title_color = QColor("#f3f4f6" if dark else "#111827")
        muted = QColor("#9ca3af" if dark else "#4b5563")
        grid_color = QColor("#2d2d2d" if dark else "#e5e7eb")
        legend_color = QColor("#e5e7eb" if dark else "#1f2937")
        legend_bg = QColor("#141414" if dark else "#f0f0f0")
        colors = self.COLORS_DARK if dark else self.COLORS_LIGHT

        painter.fillRect(self.rect(), bg)
        painter.setPen(title_color)
        painter.setFont(QFont("Segoe UI", 12, QFont.Bold))
        painter.drawText(18, 26, "Latency History")

        n_hosts = len(self.history)
        legend_w = 150 if n_hosts else 0
        left_axis, top_m, bottom_m, right_gap = 56, 44, 36, 8
        plot = self.rect().adjusted(left_axis, top_m, -(legend_w + right_gap + 6), -bottom_m)
        legend_rect = QRectF(
            self.width() - legend_w - right_gap,
            top_m,
            legend_w,
            max(0, self.height() - top_m - bottom_m),
        )

        if not self.history:
            painter.setPen(muted)
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(plot, Qt.AlignCenter, "Waiting for ICMP samples…")
            return

        valid = [v for vals in self.history.values() for v in vals if v > 0]
        max_latency = max(10.0, (max(valid) if valid else 10.0) * 1.15)

        grid = QPen(grid_color)
        painter.setPen(grid)
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            val = max_latency * (4 - i) / 4
            painter.setPen(muted)
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                QRectF(2, y - 8, left_axis - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{val:.0f} ms",
            )
            painter.setPen(grid)

        painter.setPen(QPen(grid_color))
        painter.drawRect(plot)
        painter.save()
        painter.setClipRect(plot)

        for idx, (host, values) in enumerate(self.history.items()):
            if not values:
                continue
            pen = QPen(QColor(colors[idx % len(colors)]))
            pen.setWidth(2)
            painter.setPen(pen)
            points = []
            n = len(values)
            for i, value in enumerate(values):
                x = plot.left() if n == 1 else plot.left() + plot.width() * i / (n - 1)
                y = plot.bottom() - plot.height() * min(value, max_latency) / max_latency
                points.append(QPointF(x, y))
            for i in range(1, len(points)):
                painter.drawLine(points[i - 1], points[i])
        painter.restore()

        painter.fillRect(legend_rect, legend_bg)
        painter.setPen(QPen(grid_color))
        painter.drawRect(legend_rect)

        row_h = 16
        max_rows = max(1, int(legend_rect.height() // row_h) - 1)
        items = list(self.history.items())
        shown = items[:max_rows]
        hidden = len(items) - len(shown)

        painter.setFont(QFont("Segoe UI", 8))
        for idx, (host, _values) in enumerate(shown):
            ly = legend_rect.top() + 10 + idx * row_h
            color = QColor(colors[idx % len(colors)])
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            lx = legend_rect.left() + 8
            painter.drawLine(QPointF(lx, ly), QPointF(lx + 14, ly))
            painter.setPen(legend_color)
            painter.drawText(
                QRectF(lx + 18, ly - 7, legend_w - 30, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                self._elide(host, 14),
            )

        if hidden > 0:
            ly = legend_rect.top() + 10 + len(shown) * row_h
            painter.setPen(muted)
            painter.drawText(
                QRectF(legend_rect.left() + 8, ly - 7, legend_w - 16, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"+{hidden} more…",
            )

        painter.setPen(muted)
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 10, plot.width(), 20),
            Qt.AlignCenter,
            f"Rolling window: last {self.max_points} probes",
        )


# ---------------------------------------------------------------------------
# Card-like frame helper
# ---------------------------------------------------------------------------
class CardFrame(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cardFrame")
        self.setFrameShape(QFrame.NoFrame)


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class PingMonitorApp(QMainWindow):
    PACKET_SIZE_BYTES = 56
    PING_INTERVAL_SECONDS = 1.0
    GRAPH_HISTORY_POINTS = 60
    TIMEOUT_SECONDS = 1.0
    MAX_LOG_ENTRIES = 1000
    CONFIG_FILE = "ping_monitor_config.json"

    row_update = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.hosts = []
        self.is_monitoring = False
        self.threads = []
        self.stop_event = threading.Event()
        self.host_data = {}
        self._host_items = {}
        self._last_down_signature = None

        self.packet_size_bytes = self.PACKET_SIZE_BYTES
        self.ping_interval_seconds = self.PING_INTERVAL_SECONDS
        self.graph_history_points = self.GRAPH_HISTORY_POINTS
        self.timeout_seconds = self.TIMEOUT_SECONDS

        self.setWindowTitle("Ping Check Enhanced — Network Monitor")
        self.resize(1280, 900)
        self.setMinimumSize(1050, 700)

        self._is_dark = True

        self.build_ui()
        self.apply_style()

        self.row_update.connect(self.update_row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_dashboard)
        self.timer.start(500)

        self.load_config()

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(14)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        left_spacer = QWidget()
        left_spacer.setFixedWidth(160)
        header.addWidget(left_spacer)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_col.setAlignment(Qt.AlignCenter)
        self.title_label = QLabel("Ping Check")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setObjectName("appTitle")
        self.subtitle_label = QLabel("Multi-host reachability & latency dashboard")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setObjectName("appSubtitle")
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.subtitle_label)
        header.addLayout(title_col, 1)

        right_box = QHBoxLayout()
        right_box.setSpacing(10)
        right_box.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.theme_btn = QPushButton("🌙")
        self.theme_btn.setToolTip("Toggle Light/Dark Theme")
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.theme_btn.setFixedHeight(32)
        self.theme_btn.setMinimumWidth(36)
        self.theme_btn.setMaximumWidth(44)
        self.theme_btn.setObjectName("themeBtn")

        self.status_pill = QLabel("●  IDLE")
        self.status_pill.setObjectName("statusIdle")
        self.status_pill.setFixedHeight(32)
        self.status_pill.setAlignment(Qt.AlignCenter)
        self.status_pill.setMinimumWidth(150)
        self.status_pill.setFixedWidth(150)

        right_box.addWidget(self.theme_btn)
        right_box.addWidget(self.status_pill)

        right_widget = QWidget()
        right_widget.setLayout(right_box)
        right_widget.setFixedWidth(210)
        header.addWidget(right_widget)
        root.addLayout(header)

        # Config card
        config_card = CardFrame()
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 16, 18, 16)
        config_layout.setSpacing(10)

        section = QLabel("TARGET HOSTS")
        section.setObjectName("sectionCaption")
        config_layout.addWidget(section)

        host_row = QHBoxLayout()
        host_row.setSpacing(10)
        self.host_entry = QLineEdit()
        self.host_entry.setText("8.8.8.8, 1.1.1.1, google.com")
        self.host_entry.setPlaceholderText("host1, host2, 192.168.1.1")
        self.host_entry.setClearButtonEnabled(True)
        host_row.addWidget(self.host_entry, 1)

        self.import_btn = QPushButton("📁  Import")
        self.import_btn.setToolTip("Import hosts from a TXT or CSV file")
        self.import_btn.clicked.connect(self.import_hosts_from_file)
        self.import_btn.setObjectName("secondaryBtn")
        host_row.addWidget(self.import_btn)

        self.save_config_btn = QPushButton("💾  Save Config")
        self.save_config_btn.setToolTip("Save current settings to config file")
        self.save_config_btn.clicked.connect(self.save_config)
        self.save_config_btn.setObjectName("secondaryBtn")
        host_row.addWidget(self.save_config_btn)

        config_layout.addLayout(host_row)

        settings_row = QHBoxLayout()
        settings_row.setSpacing(14)

        pkt_col = QVBoxLayout()
        pkt_col.setSpacing(4)
        pkt_lbl = QLabel("PACKET SIZE")
        pkt_lbl.setObjectName("sectionCaption")
        pkt_col.addWidget(pkt_lbl)
        self.packet_spin = QSpinBox()
        self.packet_spin.setRange(0, 65500)
        self.packet_spin.setValue(56)
        self.packet_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.packet_spin.setSuffix(" bytes")
        self.packet_spin.setMinimumWidth(140)
        pkt_col.addWidget(self.packet_spin)
        settings_row.addLayout(pkt_col)

        int_col = QVBoxLayout()
        int_col.setSpacing(4)
        int_lbl = QLabel("INTERVAL")
        int_lbl.setObjectName("sectionCaption")
        int_col.addWidget(int_lbl)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 3600.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setValue(1.0)
        self.interval_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setMinimumWidth(120)
        int_col.addWidget(self.interval_spin)
        settings_row.addLayout(int_col)

        timeout_col = QVBoxLayout()
        timeout_col.setSpacing(4)
        timeout_lbl = QLabel("TIMEOUT")
        timeout_lbl.setObjectName("sectionCaption")
        timeout_col.addWidget(timeout_lbl)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 10.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSingleStep(0.1)
        self.timeout_spin.setValue(1.0)
        self.timeout_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setMinimumWidth(120)
        timeout_col.addWidget(self.timeout_spin)
        settings_row.addLayout(timeout_col)

        hist_col = QVBoxLayout()
        hist_col.setSpacing(4)
        hist_lbl = QLabel("CHART HISTORY")
        hist_lbl.setObjectName("sectionCaption")
        hist_col.addWidget(hist_lbl)
        self.history_spin = QSpinBox()
        self.history_spin.setRange(1, 10000)
        self.history_spin.setValue(60)
        self.history_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.history_spin.setSuffix(" probes")
        self.history_spin.setMinimumWidth(140)
        hist_col.addWidget(self.history_spin)
        settings_row.addLayout(hist_col)

        settings_row.addStretch()

        self.start_btn = QPushButton("▶  Start Monitoring")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.setFixedHeight(36)
        self.start_btn.clicked.connect(self.toggle_monitoring)
        settings_row.addWidget(self.start_btn)

        self.clear_btn = QPushButton("🗑  Clear History")
        self.clear_btn.setToolTip("Clear all monitoring data and history")
        self.clear_btn.clicked.connect(self.clear_history)
        self.clear_btn.setEnabled(False)
        self.clear_btn.setObjectName("secondaryBtn")
        settings_row.addWidget(self.clear_btn)

        self.export_txt_btn = QPushButton("📄  Export TXT")
        self.export_txt_btn.setEnabled(False)
        self.export_txt_btn.clicked.connect(self.export_report_summary)
        self.export_txt_btn.setObjectName("secondaryBtn")
        settings_row.addWidget(self.export_txt_btn)

        self.export_csv_btn = QPushButton("💾  Export CSV")
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.clicked.connect(self.export_report_csv)
        self.export_csv_btn.setObjectName("secondaryBtn")
        settings_row.addWidget(self.export_csv_btn)

        config_layout.addLayout(settings_row)
        root.addWidget(config_card)

        # Search row
        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        search_label = QLabel("🔍  Filter:")
        search_label.setObjectName("bodyLabel")
        search_row.addWidget(search_label)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search hosts...")
        self.search_box.textChanged.connect(self.filter_table)
        self.search_box.setMinimumWidth(200)
        search_row.addWidget(self.search_box)
        search_row.addStretch()
        root.addLayout(search_row)

        # Main splitter
        vertical = QSplitter(Qt.Vertical)
        root.addWidget(vertical, 1)

        dash_card = CardFrame()
        dash_layout = QVBoxLayout(dash_card)
        dash_layout.setContentsMargins(14, 12, 14, 12)
        dash_layout.setSpacing(8)
        top = QHBoxLayout()
        dash_title = QLabel("Live Host Dashboard")
        dash_title.setObjectName("strongLabel")
        self.host_count_label = QLabel("0 hosts")
        self.host_count_label.setObjectName("captionLabel")
        top.addWidget(dash_title)
        top.addStretch()
        top.addWidget(self.host_count_label)
        dash_layout.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "Host / IP", "Status", "Sequence", "TTL / Type",
            "Latency", "Packet Loss", "Consecutive Fails",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(False)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(True)
        self.table.doubleClicked.connect(self.on_table_double_click)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        dash_layout.addWidget(self.table)
        vertical.addWidget(dash_card)

        lower = QSplitter(Qt.Horizontal)

        chart_card = CardFrame()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(12, 12, 12, 12)
        self.chart = LatencyChart()
        chart_layout.addWidget(self.chart)
        lower.addWidget(chart_card)

        down_card = CardFrame()
        down_layout = QVBoxLayout(down_card)
        down_layout.setContentsMargins(14, 12, 14, 12)
        down_layout.setSpacing(8)
        down_title = QLabel("Down Hosts")
        down_title.setObjectName("strongLabel")
        self.down_summary_label = QLabel("All hosts operational")
        self.down_summary_label.setObjectName("healthyLabel")
        down_layout.addWidget(down_title)
        down_layout.addWidget(self.down_summary_label)

        self.down_table = QTableWidget()
        self.down_table.setColumnCount(5)
        self.down_table.setHorizontalHeaderLabels(
            ["Host", "Status", "Fails", "Loss", "Last Failed"]
        )
        self.down_table.verticalHeader().setVisible(False)
        self.down_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        dh = self.down_table.horizontalHeader()
        dh.setSectionResizeMode(QHeaderView.Interactive)
        dh.setStretchLastSection(True)
        dh.setMinimumSectionSize(48)
        self.down_table.setColumnWidth(0, 180)
        self.down_table.setColumnWidth(1, 110)
        self.down_table.setColumnWidth(2, 50)
        self.down_table.setColumnWidth(3, 55)
        self.down_table.setColumnWidth(4, 240)
        self.down_table.setShowGrid(True)
        down_layout.addWidget(self.down_table)

        lower.addWidget(down_card)
        lower.setStretchFactor(0, 7)
        lower.setStretchFactor(1, 3)
        lower.setSizes([700, 300])
        lower.setChildrenCollapsible(False)
        vertical.addWidget(lower)
        vertical.setStretchFactor(0, 2)
        vertical.setStretchFactor(1, 3)

        self.footer_label = QLabel(
            "ICMP Echo Request  •  Real-time monitoring  •  Threaded probes"
        )
        self.footer_label.setAlignment(Qt.AlignCenter)
        self.footer_label.setObjectName("appFooter")
        root.addWidget(self.footer_label)

    # ------------------------------------------------------------------ Theme
    def toggle_theme(self):
        self._is_dark = not self._is_dark
        self.theme_btn.setText("🌙" if self._is_dark else "☀️")
        self.apply_style()
        self.chart.set_dark(self._is_dark)
        self.chart.update()
        self.refresh_dashboard()

    def apply_style(self):
        dark = self._is_dark
        bg_main = "#0b111a" if dark else "#f3f4f6"
        bg_card = "#111925" if dark else "#ffffff"
        bg_input = "#0c131e" if dark else "#ffffff"
        border_input = "#29364a" if dark else "#d1d5db"
        text_main = "#e6edf5" if dark else "#111827"
        text_muted = "#8b9cb3" if dark else "#4b5563"
        bg_table = "#0d151f" if dark else "#f9fafb"
        grid_color = "#1c2838" if dark else "#e5e7eb"
        header_bg = "#151f2d" if dark else "#f3f4f6"
        header_text = "#a8b4c4" if dark else "#374151"
        scroll_bg = "#0d151f" if dark else "#f3f4f6"
        scroll_handle = "#2b394d" if dark else "#9ca3af"
        status_idle_bg = "#2b2b2b" if dark else "#e5e7eb"
        status_idle_text = "#a0aec0" if dark else "#1f2937"
        primary = "#0078d4"
        primary_hover = "#106ebe"
        primary_pressed = "#005a9e"

        qss = f"""
        QMainWindow, QWidget {{
            background-color: {bg_main};
            color: {text_main};
        }}
        QFrame#cardFrame {{
            background-color: {bg_card};
            color: {text_main};
            border: 1px solid {border_input};
            border-radius: 12px;
        }}
        QSplitter::handle {{
            background: {bg_main};
        }}
        QLabel {{
            color: {text_main};
            background: transparent;
        }}
        QLabel#appTitle {{
            font-size: 28px;
            font-weight: 700;
            color: {"#f7fafc" if dark else "#111827"};
            background: transparent;
        }}
        QLabel#appSubtitle {{
            color: {text_muted};
            font-size: 13px;
            background: transparent;
        }}
        QLabel#appFooter {{
            color: {"#718096" if dark else "#6b7280"};
            font-size: 12px;
            background: transparent;
        }}
        QLabel#sectionCaption {{
            color: {text_muted};
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.5px;
            background: transparent;
        }}
        QLabel#strongLabel {{
            color: {text_main};
            font-weight: 700;
            font-size: 14px;
            background: transparent;
        }}
        QLabel#bodyLabel {{
            color: {text_main};
            background: transparent;
        }}
        QLabel#captionLabel {{
            color: {text_muted};
            background: transparent;
        }}
        QLabel#healthyLabel {{
            color: {"#68d391" if dark else "#16a34a"};
            font-weight: 600;
            background: transparent;
        }}
        QLineEdit, QSpinBox, QDoubleSpinBox {{
            background-color: {bg_input};
            color: {text_main};
            border: 1px solid {border_input};
            border-radius: 7px;
            padding: 6px 30px 6px 10px;
            selection-background-color: #1e4a7a;
        }}
        QSpinBox::up-button, QDoubleSpinBox::up-button,
        QSpinBox::down-button, QDoubleSpinBox::down-button {{
            subcontrol-origin: border;
            width: 24px;
            background-color: {"#26364a" if dark else "#e5e7eb"};
            border-left: 1px solid {border_input};
        }}
        QSpinBox::up-button, QDoubleSpinBox::up-button {{
            subcontrol-position: top right;
            border-top-right-radius: 6px;
            border-bottom: 1px solid {border_input};
        }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{
            subcontrol-position: bottom right;
            border-bottom-right-radius: 6px;
        }}
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
            background-color: {"#36506d" if dark else "#cbd5e1"};
        }}
        QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
        QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{
            background-color: {primary};
        }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 1px solid {primary};
        }}
        QPushButton#primaryBtn {{
            background-color: {primary};
            color: #ffffff;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            padding: 6px 16px;
        }}
        QPushButton#primaryBtn:hover {{
            background-color: {primary_hover};
        }}
        QPushButton#primaryBtn:pressed {{
            background-color: {primary_pressed};
        }}
        QPushButton#primaryBtn:disabled {{
            background-color: {"#1e3a5f" if dark else "#93c5fd"};
            color: {"#6b7c93" if dark else "#e0f2fe"};
        }}
        QPushButton#secondaryBtn {{
            background-color: {bg_card};
            color: {text_main};
            border: 1px solid {border_input};
            border-radius: 8px;
            padding: 6px 12px;
        }}
        QPushButton#secondaryBtn:hover {{
            background-color: {"#1a2433" if dark else "#f3f4f6"};
            border-color: {primary};
        }}
        QPushButton#secondaryBtn:disabled {{
            color: {text_muted};
            border-color: {border_input};
        }}
        QPushButton#themeBtn {{
            background-color: {status_idle_bg};
            color: {status_idle_text};
            border: none;
            border-radius: 12px;
            padding: 0px 8px;
            font-size: 18px;
        }}
        QPushButton#themeBtn:hover {{
            background-color: {"#3a3a3a" if dark else "#d1d5db"};
        }}
        QTableWidget {{
            background-color: {bg_table};
            color: {text_main};
            gridline-color: {grid_color};
            border: 1px solid {border_input};
            border-radius: 8px;
            outline: none;
        }}
        QTableWidget::item {{
            color: {text_main};
            background-color: {bg_table};
            padding: 6px;
        }}
        QTableWidget::item:selected {{
            background-color: #193b5c;
            color: #ffffff;
        }}
        QHeaderView::section {{
            background-color: {header_bg};
            color: {header_text};
            border: none;
            border-bottom: 1px solid {border_input};
            padding: 9px 6px;
            font-size: 11px;
            font-weight: 700;
        }}
        QHeaderView {{
            background-color: {header_bg};
        }}
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: {scroll_bg};
            border: none;
            width: 10px;
            height: 10px;
        }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: {scroll_handle};
            border-radius: 4px;
            min-height: 24px;
            min-width: 24px;
        }}
        QScrollBar::add-line, QScrollBar::sub-line {{
            height: 0;
            width: 0;
        }}
        QToolTip {{
            background-color: {bg_card};
            color: {text_main};
            border: 1px solid {border_input};
        }}
        """
        self.setStyleSheet(qss)

        # Status pill
        if not self.is_monitoring:
            self._set_status_idle()
        else:
            self._set_status_running()

        # Down summary colour may need refresh after theme change
        if self.host_data:
            down_hosts = [
                (h, d) for h, d in self.host_data.items()
                if self._is_down_status(d.get("last_status"))
            ]
            if not down_hosts:
                self.down_summary_label.setStyleSheet(
                    f"color: {'#68d391' if dark else '#16a34a'}; font-weight: 600; background: transparent;"
                )
            else:
                self.down_summary_label.setStyleSheet(
                    "color: #fc8181; font-weight: 600; background: transparent;"
                )

    def _set_status_idle(self):
        dark = self._is_dark
        bg = "#2b2b2b" if dark else "#e5e7eb"
        text = "#a0aec0" if dark else "#1f2937"
        self.status_pill.setText("●  IDLE")
        self.status_pill.setStyleSheet(
            f"background: {bg}; color: {text};"
            "border-radius: 12px; padding: 6px 14px; font-weight: 700;"
        )

    def _set_status_running(self):
        dark = self._is_dark
        bg = "#0d3b2e" if dark else "#bbf7d0"
        text = "#68d391" if dark else "#14532d"
        self.status_pill.setText("●  MONITORING")
        self.status_pill.setStyleSheet(
            f"background: {bg}; color: {text};"
            "border-radius: 12px; padding: 6px 14px; font-weight: 700;"
        )

    # ------------------------------------------------------------------ Helpers
    def _has_icmp_permissions(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
            sock.close()
            return True
        except (PermissionError, OSError):
            return False

    @staticmethod
    def _is_down_status(status):
        return status in ("Timed Out", "Unreachable", "TTL Expired")

    @staticmethod
    def _compute_host_stats(data):
        sent = data["sent"]
        received = data["received"]
        loss_pct = round(((sent - received) / sent) * 100, 1) if sent > 0 else 0.0
        valid_latencies = [l for l in data["latencies"] if l > 0]
        if valid_latencies:
            min_lat = min(valid_latencies)
            max_lat = max(valid_latencies)
            avg_lat = round(sum(valid_latencies) / len(valid_latencies), 2)
        else:
            min_lat = max_lat = avg_lat = None
        return sent, received, loss_pct, min_lat, max_lat, avg_lat

    def _open_file_dialog(self, title, name_filter, save=False, default_name="", default_suffix=""):
        dialog = QFileDialog(self, title)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setViewMode(QFileDialog.ViewMode.Detail)
        dialog.setNameFilter(name_filter)
        dialog.setOption(QFileDialog.Option.ReadOnly, not save)
        if save:
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
            if default_suffix:
                dialog.setDefaultSuffix(default_suffix)
            if default_name:
                dialog.selectFile(default_name)
        else:
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        start_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.isdir(start_dir):
            dialog.setDirectory(start_dir)
        if dialog.exec():
            selected = dialog.selectedFiles()
            if selected:
                path = selected[0]
                if default_suffix and not path.lower().endswith("." + default_suffix.lower()):
                    path = path + "." + default_suffix
                return path
        return ""

    def _msg(self, title, text, icon=QMessageBox.Information):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(icon)
        box.setStandardButtons(QMessageBox.Ok)
        # Apply a basic style so dialog matches theme
        dark = self._is_dark
        bg = "#111925" if dark else "#ffffff"
        text_c = "#e6edf5" if dark else "#111827"
        box.setStyleSheet(f"""
            QMessageBox {{ background-color: {bg}; color: {text_c}; }}
            QLabel {{ color: {text_c}; }}
            QPushButton {{
                background-color: #0078d4; color: white;
                border: none; border-radius: 6px; padding: 6px 16px; min-width: 70px;
            }}
            QPushButton:hover {{ background-color: #106ebe; }}
        """)
        box.exec()

    def _confirm(self, title, text):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(QMessageBox.Question)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        dark = self._is_dark
        bg = "#111925" if dark else "#ffffff"
        text_c = "#e6edf5" if dark else "#111827"
        box.setStyleSheet(f"""
            QMessageBox {{ background-color: {bg}; color: {text_c}; }}
            QLabel {{ color: {text_c}; }}
            QPushButton {{
                background-color: #0078d4; color: white;
                border: none; border-radius: 6px; padding: 6px 16px; min-width: 70px;
            }}
            QPushButton:hover {{ background-color: #106ebe; }}
        """)
        return box.exec() == QMessageBox.Yes

    # ------------------------------------------------------------------ Import / Config
    def import_hosts_from_file(self):
        file_path = self._open_file_dialog(
            "Import Host List",
            "Host Lists (*.txt *.csv);;Text Files (*.txt);;CSV Files (*.csv);;All Files (*)",
            save=False,
        )
        if not file_path:
            return

        try:
            hosts = []
            seen = set()

            def add_host(value):
                value = str(value).strip().strip("\"'")
                if not value:
                    return
                paren = re.search(r"\(([^()]+)\)", value)
                if paren:
                    value = paren.group(1).strip()
                if any(sep in value for sep in ("\t", ",")):
                    parts = re.split(r"[\t,]+", value)
                    candidates = [x.strip() for x in parts if x.strip()]
                    if candidates:
                        ip_candidate = next(
                            (x for x in candidates if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", x)),
                            None,
                        )
                        value = ip_candidate or candidates[-1]
                value = value.strip().strip("\"'")
                if value.lower() in {"host", "hostname", "ip", "ip address", "address"}:
                    return
                if value and value not in seen:
                    seen.add(value)
                    hosts.append(value)

            if file_path.lower().endswith(".csv"):
                with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                    rows = list(csv.reader(f))
                if rows:
                    headers = [c.strip().lower() for c in rows[0]]
                    ip_cols = [
                        i
                        for i, h in enumerate(headers)
                        if h
                        in {
                            "ip",
                            "ip address",
                            "ip_address",
                            "address",
                            "host",
                            "hostname",
                            "host/ip",
                            "hostname (ip)",
                        }
                        or "ip" in h
                    ]
                    data_rows = rows[1:] if any(headers) else rows
                    if ip_cols:
                        preferred = next(
                            (i for i, h in enumerate(headers) if h in {"ip", "ip address", "ip_address"}),
                            ip_cols[0],
                        )
                        for row in data_rows:
                            if preferred < len(row):
                                add_host(row[preferred])
                            elif row:
                                add_host(row[0])
                    else:
                        for row in rows:
                            for cell in row:
                                add_host(cell)
            else:
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            add_host(line)

            if not hosts:
                self._msg(
                    "No Hosts Found",
                    "No usable hosts/IP addresses were found in the selected file.",
                    QMessageBox.Warning,
                )
                return

            current = [h.strip() for h in self.host_entry.text().split(",") if h.strip()]
            merged = []
            seen_merged = set()
            for host in current + hosts:
                if host not in seen_merged:
                    seen_merged.add(host)
                    merged.append(host)
            self.host_entry.setText(", ".join(merged))
            self._msg(
                "Hosts Imported",
                f"Imported {len(hosts)} host(s). They will appear when monitoring starts.",
            )
        except Exception as e:
            self._msg("Import Error", f"Failed to import host list:\n{e}", QMessageBox.Critical)

    def save_config(self):
        config = {
            "hosts": self.host_entry.text(),
            "packet_size": self.packet_spin.value(),
            "interval": self.interval_spin.value(),
            "timeout": self.timeout_spin.value(),
            "history_points": self.history_spin.value(),
        }
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            self._msg("Config Saved", f"Settings saved to {self.CONFIG_FILE}")
        except Exception as e:
            self._msg("Error", f"Failed to save config:\n{e}", QMessageBox.Critical)

    def load_config(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            if "hosts" in config:
                self.host_entry.setText(config["hosts"])
            if "packet_size" in config:
                self.packet_spin.setValue(config["packet_size"])
            if "interval" in config:
                self.interval_spin.setValue(config["interval"])
            if "timeout" in config:
                self.timeout_spin.setValue(config["timeout"])
            if "history_points" in config:
                self.history_spin.setValue(config["history_points"])
        except Exception:
            pass

    # ------------------------------------------------------------------ Monitoring
    def toggle_monitoring(self):
        if self.is_monitoring:
            self.stop_monitoring()
            return

        self.hosts = [host.strip() for host in self.host_entry.text().split(",") if host.strip()]

        if not self.hosts:
            self._msg("No Hosts", "Please enter at least one host.", QMessageBox.Warning)
            return

        self.packet_size_bytes = self.packet_spin.value()
        self.ping_interval_seconds = self.interval_spin.value()
        self.graph_history_points = self.history_spin.value()
        self.timeout_seconds = self.timeout_spin.value()

        if not self._has_icmp_permissions():
            self._msg(
                "ICMP Socket Unavailable",
                "Cannot open the non-privileged Linux ICMP socket.\n\n"
                "Check net.ipv4.ping_group_range.\n\n"
                "Example:\n"
                'sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"',
                QMessageBox.Critical,
            )
            return

        self.host_data.clear()
        self._host_items.clear()
        self._last_down_signature = None
        self.table.setRowCount(0)
        self.down_table.setRowCount(0)

        for host in self.hosts:
            self.host_data[host] = {
                "latencies": [],
                "sent": 0,
                "received": 0,
                "timeline_stats": [],
                "log_rows": [],
                "tree_id": host,
                "last_status": None,
                "consecutive_fails": 0,
                "last_down_time": "-",
            }
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [host, "Starting…", "-", "-", "-", "0%", "0"]
            dark = self._is_dark
            text_color = QColor("#e6edf5" if dark else "#111827")
            bg_color = QColor("#0d151f" if dark else "#f9fafb")
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(text_color)
                item.setBackground(bg_color)
                self.table.setItem(row, col, item)
                if col == 0:
                    self._host_items[host] = item

        self.stop_event.clear()
        self.is_monitoring = True
        self.set_controls_enabled(False)
        self.start_btn.setText("⏸  Stop Monitoring")
        self._set_status_running()
        self.export_txt_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

        self.threads = []
        for host in self.hosts:
            thread = threading.Thread(target=self.ping_worker, args=(host,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop_monitoring(self):
        self.is_monitoring = False
        self.stop_event.set()
        self.threads.clear()

        self.set_controls_enabled(True)
        self.start_btn.setText("▶  Start Monitoring")
        self._set_status_idle()

        self.export_txt_btn.setEnabled(bool(self.host_data))
        self.export_csv_btn.setEnabled(bool(self.host_data))
        self.clear_btn.setEnabled(bool(self.host_data))

        if self.host_data:
            if self._confirm(
                "Export Report",
                "Monitoring stopped. Would you like to export the network summary report?",
            ):
                self.export_report_summary()

    def set_controls_enabled(self, enabled):
        self.host_entry.setEnabled(enabled)
        self.import_btn.setEnabled(enabled)
        self.save_config_btn.setEnabled(enabled)
        self.packet_spin.setEnabled(enabled)
        self.interval_spin.setEnabled(enabled)
        self.timeout_spin.setEnabled(enabled)
        self.history_spin.setEnabled(enabled)

    @Slot(str, object)
    def update_row(self, host, values):
        col0_item = self._host_items.get(host)
        if col0_item is None:
            return
        row = col0_item.row()
        if row < 0:
            return
        self.table.setSortingEnabled(False)
        dark = self._is_dark
        text_color = QColor("#e6edf5" if dark else "#111827")
        bg_color = QColor("#0d151f" if dark else "#f9fafb")
        for col, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignCenter)
            item.setForeground(text_color)
            item.setBackground(bg_color)
            self.table.setItem(row, col, item)
            if col == 0:
                self._host_items[host] = item
        status = str(values[1])
        status_item = self.table.item(row, 1)
        if status == "Connected":
            status_item.setForeground(QColor("#68d391"))
        elif status in ("Unreachable", "TTL Expired"):
            status_item.setForeground(QColor("#f6ad55"))
        else:
            status_item.setForeground(QColor("#fc8181"))
        self.table.setSortingEnabled(True)

    def filter_table(self, text):
        text = text.strip().lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                self.table.setRowHidden(row, text not in item.text().lower())

    def on_table_double_click(self, index):
        row = index.row()
        host_item = self.table.item(row, 0)
        if host_item:
            host = host_item.text()
            data = self.host_data.get(host)
            if data:
                dlg = HostDetailDialog(host, data, dark=self._is_dark, parent=self)
                dlg.exec()

    def clear_history(self):
        if self.is_monitoring:
            return
        if not self.host_data:
            return
        if not self._confirm(
            "Clear History",
            "This will remove all monitoring data for all hosts. Continue?",
        ):
            return
        self.host_data.clear()
        self._host_items.clear()
        self._last_down_signature = None
        self.table.setRowCount(0)
        self.down_table.setRowCount(0)
        self.host_count_label.setText("0 hosts")
        self.down_summary_label.setText("All hosts operational")
        dark = self._is_dark
        self.down_summary_label.setStyleSheet(
            f"color: {'#68d391' if dark else '#16a34a'}; font-weight: 600; background: transparent;"
        )
        self.chart.set_data({}, self.graph_history_points)
        self.export_txt_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self.update_footer_stats()

    def refresh_dashboard(self):
        if not self.host_data:
            self.update_footer_stats()
            return
        self.host_count_label.setText(f"{len(self.host_data)} hosts")
        self.chart.set_data(self.host_data, self.graph_history_points)
        down_hosts = [
            (host, data)
            for host, data in self.host_data.items()
            if self._is_down_status(data.get("last_status"))
        ]
        signature = tuple(
            (
                host,
                data.get("last_status"),
                data.get("consecutive_fails", 0),
                data.get("last_down_time", "-"),
            )
            for host, data in down_hosts
        )
        if signature != self._last_down_signature:
            self._last_down_signature = signature
            self.down_table.setRowCount(0)
            dark = self._is_dark
            if not down_hosts:
                self.down_summary_label.setText("All hosts operational")
                self.down_summary_label.setStyleSheet(
                    f"color: {'#68d391' if dark else '#16a34a'}; font-weight: 600; background: transparent;"
                )
            else:
                self.down_summary_label.setText(f"{len(down_hosts)} host(s) currently down")
                self.down_summary_label.setStyleSheet(
                    "color: #fc8181; font-weight: 600; background: transparent;"
                )
                text_color = QColor("#e6edf5" if dark else "#111827")
                bg_color = QColor("#0d151f" if dark else "#f9fafb")
                for host, data in down_hosts:
                    row = self.down_table.rowCount()
                    self.down_table.insertRow(row)
                    sent = data["sent"]
                    received = data["received"]
                    loss = round(((sent - received) / sent) * 100, 1) if sent else 0.0
                    values = [
                        host,
                        data.get("last_status", "Down"),
                        data.get("consecutive_fails", 0),
                        f"{loss}%",
                        data.get("last_down_time", "-"),
                    ]
                    for col, value in enumerate(values):
                        item = QTableWidgetItem(str(value))
                        item.setTextAlignment(Qt.AlignCenter)
                        item.setForeground(text_color)
                        item.setBackground(bg_color)
                        self.down_table.setItem(row, col, item)
        self.update_footer_stats()

    def update_footer_stats(self):
        if not self.host_data:
            self.footer_label.setText(
                "ICMP Echo Request  •  Real-time monitoring  •  Threaded probes"
            )
            return
        total_sent = sum(d["sent"] for d in self.host_data.values())
        total_recv = sum(d["received"] for d in self.host_data.values())
        overall_loss = ((total_sent - total_recv) / total_sent * 100) if total_sent else 0.0
        self.footer_label.setText(
            f"Total Hosts: {len(self.host_data)}  |  Overall Loss: {overall_loss:.1f}%  |  "
            "ICMP Echo Request  •  Real-time monitoring"
        )

    def ping_worker(self, host):
        local_seq = 0
        ip_recvttl = getattr(socket, "IP_RECVTTL", 12)
        ip_ttl_cmsg_type = getattr(socket, "IP_TTL", 2)
        cmsg_bufsize = socket.CMSG_SPACE(struct.calcsize("i"))

        while self.is_monitoring:
            local_seq += 1
            seq = local_seq & 0xFFFF
            self.host_data[host]["sent"] += 1
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            status = "Timed Out"
            ttl_val = "-"
            latency = 0

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
                sock.settimeout(self.timeout_seconds)
                sock.setsockopt(socket.IPPROTO_IP, ip_recvttl, 1)
                sock.connect((host, 1))

                icmp_id = sock.getsockname()[1] & 0xFFFF
                packet = _build_icmp_echo_packet(icmp_id, seq, self.packet_size_bytes)

                start_time = time.time()
                sock.send(packet)

                while True:
                    data, ancdata, _flags, _addr = sock.recvmsg(1024, cmsg_bufsize)
                    end_time = time.time()

                    if len(data) < 8:
                        continue

                    icmp_type, icmp_code, _chk, r_id, r_seq = struct.unpack("!BBHHH", data[:8])

                    ttl = None
                    for cmsg_level, cmsg_type, cmsg_data in ancdata:
                        if cmsg_level == socket.IPPROTO_IP and cmsg_type == ip_ttl_cmsg_type:
                            ttl = struct.unpack("@i", cmsg_data[:4])[0]

                    if icmp_type == 0 and r_id == icmp_id and r_seq == seq:
                        latency = round((end_time - start_time) * 1000, 2)
                        status = "Connected"
                        ttl_val = str(ttl) if ttl is not None else "N/A"
                        break

                    if icmp_type in (3, 11) and r_id == icmp_id:
                        status = "Unreachable" if icmp_type == 3 else "TTL Expired"
                        break

                sock.close()

            except (socket.timeout, PermissionError, OSError):
                status = "Timed Out"
                ttl_val = "-"

            hd = self.host_data[host]
            if status == "Connected":
                hd["received"] += 1
                hd["latencies"].append(latency)
                latency_str = f"{latency} ms"
                hd["timeline_stats"].append(
                    f"[{timestamp}] Seq {local_seq}: Success - Latency {latency}ms - TTL {ttl_val}"
                )
                hd["log_rows"].append((local_seq, timestamp, "Success", latency))
                hd["consecutive_fails"] = 0
            else:
                latency_str = "Timeout" if status == "Timed Out" else status
                hd["latencies"].append(0)
                hd["timeline_stats"].append(
                    f"[{timestamp}] Seq {local_seq}: FAILED - {status}"
                )
                hd["log_rows"].append((local_seq, timestamp, status, ""))
                hd["consecutive_fails"] += 1
                hd["last_down_time"] = timestamp

            if len(hd["timeline_stats"]) > self.MAX_LOG_ENTRIES:
                hd["timeline_stats"] = hd["timeline_stats"][-self.MAX_LOG_ENTRIES :]
            if len(hd["log_rows"]) > self.MAX_LOG_ENTRIES:
                hd["log_rows"] = hd["log_rows"][-self.MAX_LOG_ENTRIES :]

            hd["last_status"] = status

            sent = hd["sent"]
            received = hd["received"]
            loss_pct = f"{round(((sent - received) / sent) * 100, 1)}%" if sent else "0%"

            row_values = (
                host,
                status,
                str(local_seq),
                ttl_val,
                latency_str,
                loss_pct,
                str(hd["consecutive_fails"]),
            )
            self.row_update.emit(host, row_values)

            self.stop_event.wait(self.ping_interval_seconds)

    # ------------------------------------------------------------------ Export
    def export_report_summary(self):
        if not self.host_data:
            self._msg("No Data", "There is no monitoring data to export yet.", QMessageBox.Warning)
            return

        file_path = self._open_file_dialog(
            "Save Packet Summary Report",
            "Text Files (*.txt);;Log Files (*.log);;All Files (*)",
            save=True,
            default_name="ping_check_report.txt",
            default_suffix="txt",
        )
        if not file_path:
            return

        data_snapshot = {host: dict(data) for host, data in self.host_data.items()}

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("=" * 65 + "\n")
                f.write("           NETWORK PERFORMANCE SUMMARY REPORT\n")
                f.write(f"           Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 65 + "\n\n")

                f.write("[1. HOST AGGREGATED METRICS]\n")
                f.write("-" * 80 + "\n")
                f.write(
                    f"{'Target Host':<20} | {'Sent':<6} | {'Recv':<6} | {'Loss %':<8} | "
                    f"{'Min (ms)':<9} | {'Max (ms)':<9} | {'Avg (ms)':<9}\n"
                )
                f.write("-" * 80 + "\n")

                for host, data in data_snapshot.items():
                    sent, received, loss_pct, min_lat, max_lat, avg_lat = self._compute_host_stats(data)
                    if min_lat is None:
                        min_lat, max_lat, avg_lat = "-", "-", "-"
                    f.write(
                        f"{host:<20} | {sent:<6} | {received:<6} | {str(loss_pct) + '%':<8} | "
                        f"{min_lat:<9} | {max_lat:<9} | {avg_lat:<9}\n"
                    )
                f.write("-" * 80 + "\n\n")

                f.write("[2. HOSTS DOWN AT TIME OF EXPORT]\n")
                f.write("-" * 45 + "\n")
                down_now = [
                    h for h, d in data_snapshot.items() if self._is_down_status(d.get("last_status"))
                ]
                if down_now:
                    for h in down_now:
                        f.write(
                            f"- {h} (last failed at {data_snapshot[h].get('last_down_time', '-')})\n"
                        )
                else:
                    f.write("All hosts were up.\n")
                f.write("\n")

                f.write("[3. DETAILED SEQUENCE TIMELINE LOGS]\n")
                for host, data in data_snapshot.items():
                    f.write(f"\n# Target Stream: {host}\n")
                    f.write("-" * 45 + "\n")
                    for log_entry in data["timeline_stats"]:
                        f.write(log_entry + "\n")

            self._msg("Success", f"Report exported to:\n{file_path}")
        except Exception as e:
            self._msg("Error", f"Failed to write file summary:\n{str(e)}", QMessageBox.Critical)

    def export_report_csv(self):
        if not self.host_data:
            self._msg("No Data", "There is no monitoring data to export yet.", QMessageBox.Warning)
            return

        file_path = self._open_file_dialog(
            "Save Packet Summary Report (CSV)",
            "CSV Files (*.csv);;All Files (*)",
            save=True,
            default_name="ping_check_report.csv",
            default_suffix="csv",
        )
        if not file_path:
            return

        if not file_path.lower().endswith(".csv"):
            file_path = file_path + ".csv"

        data_snapshot = {host: dict(data) for host, data in self.host_data.items()}

        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, dialect="excel", quoting=csv.QUOTE_MINIMAL)

                writer.writerow([
                    "Section",
                    "Host",
                    "Sent",
                    "Received",
                    "Loss_Pct",
                    "Min_ms",
                    "Max_ms",
                    "Avg_ms",
                    "Last_Status",
                    "Consecutive_Fails",
                    "Last_Down_At",
                    "Generated_On",
                ])
                generated = time.strftime("%Y-%m-%d %H:%M:%S")
                for host, data in data_snapshot.items():
                    sent, received, loss_pct, min_lat, max_lat, avg_lat = self._compute_host_stats(data)
                    if min_lat is None:
                        min_lat, max_lat, avg_lat = "", "", ""
                    writer.writerow([
                        "Summary",
                        host,
                        sent,
                        received,
                        loss_pct,
                        min_lat,
                        max_lat,
                        avg_lat,
                        data.get("last_status") or "-",
                        data.get("consecutive_fails", 0),
                        data.get("last_down_time", "-"),
                        generated,
                    ])

                down_now = [
                    (h, d)
                    for h, d in data_snapshot.items()
                    if self._is_down_status(d.get("last_status"))
                ]
                for host, data in down_now:
                    sent, received, loss_pct, _min, _max, _avg = self._compute_host_stats(data)
                    writer.writerow([
                        "Down",
                        host,
                        sent,
                        received,
                        loss_pct,
                        "",
                        "",
                        "",
                        data.get("last_status") or "Down",
                        data.get("consecutive_fails", 0),
                        data.get("last_down_time", "-"),
                        generated,
                    ])

                writer.writerow([])
                writer.writerow([
                    "Section",
                    "Host",
                    "Sequence",
                    "Timestamp",
                    "Result",
                    "Latency_ms",
                ])
                for host, data in data_snapshot.items():
                    for seq, timestamp, result, latency in data["log_rows"]:
                        writer.writerow(["Probe", host, seq, timestamp, result, latency])

            self._msg(
                "Success",
                f"CSV report exported to:\n{file_path}\n\nOpen with LibreOffice Calc or Excel.",
            )
        except Exception as e:
            self._msg("Error", f"Failed to write CSV report:\n{str(e)}", QMessageBox.Critical)

    def closeEvent(self, event):
        self.is_monitoring = False
        self.stop_event.set()
        event.accept()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # This application uses raw ICMP sockets and never needs desktop HTTP
    # proxy discovery. Disabling it avoids GLib trying to construct a proxy
    # from an empty system address during Qt startup.
    QNetworkProxyFactory.setUseSystemConfiguration(False)
    app = QApplication(sys.argv)
    app.setApplicationName("Ping Check Enhanced")
    # Default dark palette so the window looks correct before styles apply
    palette = QPalette()
    bg = QColor("#0b111a")
    panel = QColor("#111925")
    text = QColor("#e6edf5")
    muted = QColor("#8b9cb3")
    highlight = QColor("#193b5c")
    base = QColor("#0d151f")
    palette.setColor(QPalette.Window, bg)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, panel)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, panel)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.Highlight, highlight)
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, panel)
    palette.setColor(QPalette.ToolTipText, text)
    palette.setColor(QPalette.PlaceholderText, muted)
    palette.setColor(QPalette.Link, QColor("#63b3ed"))
    app.setPalette(palette)

    window = PingMonitorApp()
    window.show()
    try:
        exit_code = app.exec()
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
