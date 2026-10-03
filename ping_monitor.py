#!/usr/bin/env python3
"""
Ping Check Enhanced - Multi-Host Network Monitor (Pure PySide6)
Professional Desktop Edition
"""

import csv
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, Slot, QTimer, QPointF, QRectF, QThreadPool
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QGridLayout, QSplitter, QFileDialog, QAbstractItemView, QHeaderView,
    QTableWidget, QTableWidgetItem, QLineEdit, QDialog, QTextEdit,
    QPushButton, QSpinBox, QDoubleSpinBox, QFrame, QMessageBox,
)
from probes import ProbeResult, ProbeTask, normalize_targets


class SortableTableWidgetItem(QTableWidgetItem):
    """Sort numbers by value while preserving normal text sorting."""

    def __init__(self, text, numeric=False):
        super().__init__(text)
        self.numeric = numeric

    def __lt__(self, other):
        if self.numeric and getattr(other, "numeric", False):
            left = re.sub(r"[^0-9.+-]", "", self.text())
            right = re.sub(r"[^0-9.+-]", "", other.text())
            try:
                return float(left) < float(right)
            except ValueError:
                pass
        return self.text().casefold() < other.text().casefold()


# ---------------------------------------------------------------------------
# Host Detail Dialog
# ---------------------------------------------------------------------------
class HostDetailDialog(QDialog):
    def __init__(self, host, data, dark=True, parent=None):
        super().__init__(parent)
        self.host = host
        self.data = data
        self._dark = dark
        self.setWindowTitle(f"Host Inspection — {host}")
        self.resize(640, 540)
        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QLabel(f"Target: {self.host}")
        header.setObjectName("detailTitle")
        layout.addWidget(header)

        stats_layout = QGridLayout()
        stats_layout.setSpacing(12)
        sent = self.data.get("sent", 0)
        received = self.data.get("received", 0)
        loss_pct = round(((sent - received) / sent) * 100, 1) if sent > 0 else 0.0
        valid_latencies = [l for l in self.data.get("latencies", []) if l > 0]
        stats = [
            ("Packets Sent", sent),
            ("Packets Received", received),
            ("Packet Loss", f"{loss_pct}%"),
            ("Recent Samples", len(valid_latencies)),
        ]
        if valid_latencies:
            stats.extend([
                ("Min Latency", f"{min(valid_latencies):.2f} ms"),
                ("Max Latency", f"{max(valid_latencies):.2f} ms"),
                ("Avg Latency", f"{sum(valid_latencies) / len(valid_latencies):.2f} ms"),
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
        status_color = "#3fb950" if status == "Connected" else "#f85149"
        status_row = QHBoxLayout()
        status_label = QLabel("Current Status:")
        status_label.setObjectName("mutedLabel")
        status_value = QLabel(status)
        status_value.setStyleSheet(f"color: {status_color}; font-weight: 600;")
        status_row.addWidget(status_label)
        status_row.addWidget(status_value)
        status_row.addStretch()
        layout.addLayout(status_row)

        if self.data.get("last_down_time") and self.data.get("last_down_time") != "-":
            down_lbl = QLabel(f"Last Down Event: {self.data['last_down_time']}")
            down_lbl.setObjectName("mutedLabel")
            layout.addWidget(down_lbl)

        timeline_title = QLabel("Recent Probing Timeline (Last 50):")
        timeline_title.setObjectName("strongLabel")
        layout.addWidget(timeline_title)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(200)
        logs = self.data.get("timeline_stats", [])
        self.log_text.setText("\n".join(logs[-50:]) if logs else "No timeline logs available.")
        layout.addWidget(self.log_text)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setObjectName("secondaryBtn")
        close_btn.setFixedHeight(34)
        close_btn.setMinimumWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _apply_style(self):
        dark = self._dark
        bg = "#0d1117" if dark else "#ffffff"
        text = "#e6edf3" if dark else "#1f2328"
        muted = "#848d97" if dark else "#656d76"
        input_bg = "#161b22" if dark else "#f6f8fa"
        border = "#30363d" if dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg};
                color: {text};
            }}
            QLabel#detailTitle {{
                font-size: 16px;
                font-weight: 600;
                color: {text};
                background: transparent;
            }}
            QLabel#mutedLabel {{
                color: {muted};
                background: transparent;
            }}
            QLabel#strongLabel {{
                color: {text};
                font-weight: 600;
                background: transparent;
            }}
            QTextEdit {{
                background-color: {input_bg};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 8px;
                font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
                font-size: 11px;
            }}
            QPushButton#secondaryBtn {{
                background-color: {"#21262d" if dark else "#f6f8fa"};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                font-weight: 500;
                padding: 6px 14px;
            }}
            QPushButton#secondaryBtn:hover {{
                background-color: {"#30363d" if dark else "#edf2f7"};
            }}
        """)


# ---------------------------------------------------------------------------
# Latency Chart
# ---------------------------------------------------------------------------
class LatencyChart(QWidget):
    COLORS_DARK = [
        "#58a6ff", "#3fb950", "#d29922", "#f85149",
        "#bc8cff", "#39c5cf", "#db61a2", "#8b949e",
        "#79c0ff", "#56d364", "#e3b341", "#ff7b72",
        "#d2a8ff", "#56d4dd", "#ff7bca", "#8b949e",
    ]
    COLORS_LIGHT = [
        "#0969da", "#1f883d", "#9a6700", "#cf222e",
        "#8250df", "#1b7c83", "#bf3989", "#57606a",
        "#0550ae", "#1a7f37", "#845300", "#a40e26",
        "#6639ba", "#135f65", "#952d75", "#57606a",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = {}
        self.max_points = 60
        self._dark = True
        self.setMinimumHeight(240)

    def set_dark(self, dark: bool):
        self._dark = dark
        self.update()

    def set_data(self, host_data, max_points):
        self.max_points = max(1, int(max_points))
        self.history = {
            host: list(data["latencies"])[-self.max_points:]
            for host, data in host_data.items()
            if data.get("latencies")
        }
        self.update()

    @staticmethod
    def _elide(text, max_chars=16):
        text = str(text)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def paintEvent(self, _event):
        _event.accept()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = self._dark
        bg = QColor("#0d1117" if dark else "#ffffff")
        title_color = QColor("#e6edf3" if dark else "#1f2328")
        muted = QColor("#848d97" if dark else "#656d76")
        grid_color = QColor("#21262d" if dark else "#eaeef2")
        legend_color = QColor("#e6edf3" if dark else "#1f2328")
        legend_bg = QColor("#161b22" if dark else "#f6f8fa")
        colors = self.COLORS_DARK if dark else self.COLORS_LIGHT

        painter.fillRect(self.rect(), bg)
        painter.setPen(title_color)
        painter.setFont(QFont("Sans Serif", 11, QFont.Bold))
        painter.drawText(16, 22, "Latency History")

        n_hosts = len(self.history)
        legend_w = 140 if n_hosts else 0
        left_axis, top_m, bottom_m, right_gap = 52, 38, 30, 8
        plot = self.rect().adjusted(left_axis, top_m, -(legend_w + right_gap + 4), -bottom_m)
        legend_rect = QRectF(
            self.width() - legend_w - right_gap,
            top_m,
            legend_w,
            max(0, self.height() - top_m - bottom_m),
        )

        if not self.history:
            painter.setPen(muted)
            painter.setFont(QFont("Sans Serif", 10))
            painter.drawText(plot, Qt.AlignCenter, "Waiting for ICMP telemetry samples...")
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
            painter.setFont(QFont("Sans Serif", 8))
            painter.drawText(
                QRectF(2, y - 8, left_axis - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{val:.0f}ms",
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

        painter.setFont(QFont("Sans Serif", 8))
        for idx, item in enumerate(shown):
            host = item[0]
            ly = legend_rect.top() + 8 + idx * row_h
            color = QColor(colors[idx % len(colors)])
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            lx = legend_rect.left() + 6
            painter.drawLine(QPointF(lx, ly), QPointF(lx + 12, ly))
            painter.setPen(legend_color)
            painter.drawText(
                QRectF(lx + 16, ly - 7, legend_w - 24, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                self._elide(host, 13),
            )

        if hidden > 0:
            ly = legend_rect.top() + 8 + len(shown) * row_h
            painter.setPen(muted)
            painter.drawText(
                QRectF(legend_rect.left() + 6, ly - 7, legend_w - 12, 14),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"+{hidden} more...",
            )

        painter.setPen(muted)
        painter.setFont(QFont("Sans Serif", 8))
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 6, plot.width(), 18),
            Qt.AlignCenter,
            f"Buffer window: last {self.max_points} probes",
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
    CONFIG_FILE = Path(__file__).resolve().with_name("ping_monitor_config.json")

    def __init__(self):
        super().__init__()
        self.hosts = []
        self.config_warning = None
        self.is_monitoring = False
        self._run_id = 0
        self.probe_task = None
        self._stop_pending = False
        self.probe_pool = QThreadPool(self)
        self.probe_pool.setMaxThreadCount(1)
        self.host_data = {}
        self._host_items = {}
        self._last_down_signature = None

        self.packet_size_bytes = self.PACKET_SIZE_BYTES
        self.ping_interval_seconds = self.PING_INTERVAL_SECONDS
        self.graph_history_points = self.GRAPH_HISTORY_POINTS
        self.timeout_seconds = self.TIMEOUT_SECONDS

        self.setWindowTitle("Ping Check — Professional Network Monitor")
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(1260, int(available.width() * 0.92))
            height = min(880, int(available.height() * 0.9))
            self.resize(width, height)
            self.setMinimumSize(min(760, width), min(520, height))
        else:
            self.resize(1100, 740)
            self.setMinimumSize(760, 520)

        self._is_dark = True

        self.build_ui()
        self.apply_style()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_dashboard)
        self.timer.start(1000)

        self.load_config()

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Header bar
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        self.title_label = QLabel("Network Reachability Monitor")
        self.title_label.setObjectName("appTitle")
        self.subtitle_label = QLabel("Real-time ICMP telemetry & latency diagnostic tool")
        self.subtitle_label.setObjectName("appSubtitle")
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.subtitle_label)
        header.addLayout(title_col, 1)

        right_box = QHBoxLayout()
        right_box.setSpacing(10)
        right_box.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.theme_btn = QPushButton()
        self.theme_btn.setFixedSize(34, 30)
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.theme_btn.setObjectName("secondaryBtn")

        self.status_pill = QLabel("IDLE")
        self.status_pill.setObjectName("statusIdle")
        self.status_pill.setFixedHeight(30)
        self.status_pill.setAlignment(Qt.AlignCenter)
        self.status_pill.setMinimumWidth(110)

        right_box.addWidget(self.theme_btn)
        right_box.addWidget(self.status_pill)
        header.addLayout(right_box)
        root.addLayout(header)

        # Config card
        config_card = CardFrame()
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(16, 14, 16, 14)
        config_layout.setSpacing(10)

        section = QLabel("TARGET CONFIGURATION")
        section.setObjectName("sectionCaption")
        config_layout.addWidget(section)

        host_row = QHBoxLayout()
        host_row.setSpacing(8)
        self.host_entry = QLineEdit()
        self.host_entry.setText("8.8.8.8, 1.1.1.1, google.com")
        self.host_entry.setPlaceholderText("Enter hosts separated by commas (e.g. 8.8.8.8, example.com)")
        self.host_entry.setClearButtonEnabled(True)
        host_row.addWidget(self.host_entry, 1)

        self.import_btn = QPushButton("Import List")
        self.import_btn.clicked.connect(self.import_hosts_from_file)
        self.import_btn.setObjectName("secondaryBtn")
        host_row.addWidget(self.import_btn)

        self.save_config_btn = QPushButton("Save Config")
        self.save_config_btn.clicked.connect(self.save_config)
        self.save_config_btn.setObjectName("secondaryBtn")
        host_row.addWidget(self.save_config_btn)

        config_layout.addLayout(host_row)

        settings_row = QHBoxLayout()
        settings_row.setSpacing(12)

        pkt_col = QVBoxLayout()
        pkt_col.setSpacing(3)
        pkt_lbl = QLabel("PACKET SIZE")
        pkt_lbl.setObjectName("sectionCaption")
        pkt_col.addWidget(pkt_lbl)
        self.packet_spin = QSpinBox()
        self.packet_spin.setRange(0, 65500)
        self.packet_spin.setValue(56)
        self.packet_spin.setSuffix(" B")
        self.packet_spin.setMinimumWidth(120)
        pkt_col.addWidget(self.packet_spin)
        settings_row.addLayout(pkt_col)

        int_col = QVBoxLayout()
        int_col.setSpacing(3)
        int_lbl = QLabel("INTERVAL")
        int_lbl.setObjectName("sectionCaption")
        int_col.addWidget(int_lbl)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 3600.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setValue(1.0)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setMinimumWidth(110)
        int_col.addWidget(self.interval_spin)
        settings_row.addLayout(int_col)

        timeout_col = QVBoxLayout()
        timeout_col.setSpacing(3)
        timeout_lbl = QLabel("TIMEOUT")
        timeout_lbl.setObjectName("sectionCaption")
        timeout_col.addWidget(timeout_lbl)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 10.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSingleStep(0.1)
        self.timeout_spin.setValue(1.0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setMinimumWidth(110)
        timeout_col.addWidget(self.timeout_spin)
        settings_row.addLayout(timeout_col)

        hist_col = QVBoxLayout()
        hist_col.setSpacing(3)
        hist_lbl = QLabel("CHART HISTORY")
        hist_lbl.setObjectName("sectionCaption")
        hist_col.addWidget(hist_lbl)
        self.history_spin = QSpinBox()
        self.history_spin.setRange(1, 10000)
        self.history_spin.setValue(60)
        self.history_spin.setSuffix(" pts")
        self.history_spin.setMinimumWidth(120)
        hist_col.addWidget(self.history_spin)
        settings_row.addLayout(hist_col)

        settings_row.addStretch()
        config_layout.addLayout(settings_row)

        actions_row = QHBoxLayout()
        actions_row.addStretch()

        self.start_btn = QPushButton("Start Monitoring")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.setFixedHeight(34)
        self.start_btn.clicked.connect(self.toggle_monitoring)
        actions_row.addWidget(self.start_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_history)
        self.clear_btn.setEnabled(False)
        self.clear_btn.setObjectName("secondaryBtn")
        actions_row.addWidget(self.clear_btn)

        self.export_txt_btn = QPushButton("Export TXT")
        self.export_txt_btn.setEnabled(False)
        self.export_txt_btn.clicked.connect(self.export_report_summary)
        self.export_txt_btn.setObjectName("secondaryBtn")
        actions_row.addWidget(self.export_txt_btn)

        self.export_csv_btn = QPushButton("Export CSV")
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.clicked.connect(self.export_report_csv)
        self.export_csv_btn.setObjectName("secondaryBtn")
        actions_row.addWidget(self.export_csv_btn)

        config_layout.addLayout(actions_row)
        root.addWidget(config_card)

        # Search row
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_label = QLabel("Filter:")
        search_label.setObjectName("bodyLabel")
        search_row.addWidget(search_label)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter targets...")
        self.search_box.textChanged.connect(self.filter_table)
        self.search_box.setMinimumWidth(220)
        search_row.addWidget(self.search_box)
        search_row.addStretch()
        root.addLayout(search_row)

        # Main splitter
        vertical = QSplitter(Qt.Vertical)
        root.addWidget(vertical, 1)

        dash_card = CardFrame()
        dash_layout = QVBoxLayout(dash_card)
        dash_layout.setContentsMargins(14, 12, 14, 12)
        dash_layout.setSpacing(6)
        top = QHBoxLayout()
        dash_title = QLabel("Live Telemetry Table")
        dash_title.setObjectName("strongLabel")
        self.host_count_label = QLabel("0 hosts")
        self.host_count_label.setObjectName("captionLabel")
        top.addWidget(dash_title)
        top.addStretch()
        top.addWidget(self.host_count_label)
        dash_layout.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Host / IP", "Status", "Seq", "Latency", "Packet Loss", "Failures",
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
        chart_layout.setContentsMargins(10, 10, 10, 10)
        self.chart = LatencyChart()
        chart_layout.addWidget(self.chart)
        lower.addWidget(chart_card)

        down_card = CardFrame()
        down_layout = QVBoxLayout(down_card)
        down_layout.setContentsMargins(14, 12, 14, 12)
        down_layout.setSpacing(6)
        down_title = QLabel("Degraded / Down Hosts")
        down_title.setObjectName("strongLabel")
        self.down_summary_label = QLabel("No targets monitored")
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
        self.down_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.down_table.setSortingEnabled(True)
        dh = self.down_table.horizontalHeader()
        dh.setSectionResizeMode(QHeaderView.Interactive)
        dh.setStretchLastSection(True)
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

        self.footer_label = QLabel("ICMP Echo Prober • Ready")
        self.footer_label.setAlignment(Qt.AlignCenter)
        self.footer_label.setObjectName("appFooter")
        root.addWidget(self.footer_label)

    # ------------------------------------------------------------------ Theme
    def toggle_theme(self):
        self._is_dark = not self._is_dark
        self.apply_style()
        self.chart.set_dark(self._is_dark)
        self.chart.update()
        self.refresh_dashboard()

    def apply_style(self):
        dark = self._is_dark
        bg_main = "#0d1117" if dark else "#f6f8fa"
        bg_card = "#161b22" if dark else "#ffffff"
        bg_input = "#0d1117" if dark else "#ffffff"
        border_input = "#30363d" if dark else "#d0d7de"
        text_main = "#e6edf3" if dark else "#1f2328"
        text_muted = "#848d97" if dark else "#656d76"
        bg_table = "#0d1117" if dark else "#ffffff"
        grid_color = "#21262d" if dark else "#eaeef2"
        header_bg = "#161b22" if dark else "#f6f8fa"
        header_text = "#848d97" if dark else "#57606a"
        scroll_bg = "#0d1117" if dark else "#f6f8fa"
        scroll_handle = "#30363d" if dark else "#8c959f"
        primary = "#238636" if dark else "#1f883d"
        primary_hover = "#2ea043" if dark else "#1a7f37"

        qss = f"""
        QMainWindow, QWidget {{
            background-color: {bg_main};
            color: {text_main};
            font-family: "Sans Serif";
            font-size: 12px;
        }}
        QFrame#cardFrame {{
            background-color: {bg_card};
            color: {text_main};
            border: 1px solid {border_input};
            border-radius: 6px;
        }}
        QSplitter::handle {{
            background: {bg_main};
        }}
        QLabel {{
            color: {text_main};
            background: transparent;
        }}
        QLabel#appTitle {{
            font-size: 18px;
            font-weight: 600;
            color: {text_main};
        }}
        QLabel#appSubtitle {{
            color: {text_muted};
            font-size: 11px;
        }}
        QLabel#appFooter {{
            color: {text_muted};
            font-size: 11px;
        }}
        QLabel#sectionCaption {{
            color: {text_muted};
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.4px;
        }}
        QLabel#strongLabel {{
            color: {text_main};
            font-weight: 600;
            font-size: 13px;
        }}
        QLabel#bodyLabel {{
            color: {text_main};
        }}
        QLabel#captionLabel {{
            color: {text_muted};
        }}
        QLabel#healthyLabel {{
            color: {"#3fb950" if dark else "#1a7f37"};
            font-weight: 600;
        }}
        QLineEdit, QSpinBox, QDoubleSpinBox {{
            background-color: {bg_input};
            color: {text_main};
            border: 1px solid {border_input};
            border-radius: 6px;
            padding: 5px 8px;
            selection-background-color: #388bfd;
            selection-color: #ffffff;
        }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 1px solid #388bfd;
        }}
        QPushButton#primaryBtn {{
            background-color: {primary};
            color: #ffffff;
            border: none;
            border-radius: 6px;
            font-weight: 600;
            padding: 5px 14px;
        }}
        QPushButton#primaryBtn:hover {{
            background-color: {primary_hover};
        }}
        QPushButton#primaryBtn:disabled {{
            background-color: {"#111a16" if dark else "#bad8b6"};
            color: {"#484f58" if dark else "#8c959f"};
        }}
        QPushButton#secondaryBtn {{
            background-color: {"#21262d" if dark else "#f6f8fa"};
            color: {text_main};
            border: 1px solid {border_input};
            border-radius: 6px;
            font-weight: 500;
            padding: 5px 12px;
        }}
        QPushButton#secondaryBtn:hover {{
            background-color: {"#30363d" if dark else "#f3f4f6"};
            border-color: {"#8b949e" if dark else "#8c959f"};
        }}
        QPushButton#secondaryBtn:disabled {{
            color: {text_muted};
            border-color: {border_input};
        }}
        QTableWidget {{
            background-color: {bg_table};
            color: {text_main};
            gridline-color: {grid_color};
            border: 1px solid {border_input};
            border-radius: 6px;
            outline: none;
        }}
        QTableWidget::item {{
            color: {text_main};
            background-color: {bg_table};
            padding: 5px;
        }}
        QTableWidget::item:selected {{
            background-color: {"#1f6feb" if dark else "#0969da"};
            color: #ffffff;
        }}
        QHeaderView::section {{
            background-color: {header_bg};
            color: {header_text};
            border: none;
            border-bottom: 1px solid {border_input};
            padding: 6px;
            font-size: 11px;
            font-weight: 600;
        }}
        QHeaderView {{
            background-color: {header_bg};
        }}
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: {scroll_bg};
            border: none;
            width: 8px;
            height: 8px;
        }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: {scroll_handle};
            border-radius: 4px;
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
        theme_name = "Dark" if dark else "Light"
        self.theme_btn.setText("☾" if dark else "☀")
        self.theme_btn.setToolTip(f"{theme_name} theme selected. Click to switch.")
        self.theme_btn.setAccessibleName(f"{theme_name} theme selected")

        if not self.is_monitoring:
            self._set_status_idle()
        else:
            self._set_status_running()

        if self.host_data:
            down_hosts = [
                (h, d) for h, d in self.host_data.items()
                if self._is_down_status(d.get("last_status"))
            ]
            if not down_hosts:
                self.down_summary_label.setStyleSheet(
                    f"color: {'#3fb950' if dark else '#1a7f37'}; font-weight: 600; background: transparent;"
                )
            else:
                self.down_summary_label.setStyleSheet(
                    "color: #f85149; font-weight: 600; background: transparent;"
                )

    def _set_status_idle(self):
        dark = self._is_dark
        bg = "#21262d" if dark else "#eaeef2"
        text = "#848d97" if dark else "#57606a"
        self.status_pill.setText("IDLE")
        self.status_pill.setStyleSheet(
            f"background: {bg}; color: {text};"
            "border-radius: 4px; font-weight: 600;"
        )

    def _set_status_running(self):
        dark = self._is_dark
        bg = "#113824" if dark else "#dafbe1"
        text = "#3fb950" if dark else "#1a7f37"
        self.status_pill.setText("MONITORING")
        self.status_pill.setStyleSheet(
            f"background: {bg}; color: {text};"
            "border-radius: 4px; font-weight: 600;"
        )

    # ------------------------------------------------------------------ Helpers
    @staticmethod
    def _is_down_status(status):
        return status in ("Timed Out", "Unreachable", "DNS Error", "Permission Denied", "Probe Error")

    @staticmethod
    def _compute_host_stats(data):
        sent = data["sent"]
        received = data["received"]
        loss_pct = round(((sent - received) / sent) * 100, 1) if sent > 0 else 0.0
        latency_count = data.get("latency_count", 0)
        if latency_count:
            min_lat = data["min_latency"]
            max_lat = data["max_latency"]
            avg_lat = round(data["latency_sum"] / latency_count, 2)
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
        dark = self._is_dark
        bg = "#0d1117" if dark else "#ffffff"
        text_c = "#e6edf3" if dark else "#1f2328"
        box.setStyleSheet(f"""
            QMessageBox {{ background-color: {bg}; color: {text_c}; }}
            QLabel {{ color: {text_c}; }}
            QPushButton {{
                background-color: {"#238636" if dark else "#1f883d"}; color: white;
                border: none; border-radius: 6px; padding: 5px 14px; min-width: 60px;
            }}
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
        bg = "#0d1117" if dark else "#ffffff"
        text_c = "#e6edf3" if dark else "#1f2328"
        box.setStyleSheet(f"""
            QMessageBox {{ background-color: {bg}; color: {text_c}; }}
            QLabel {{ color: {text_c}; }}
            QPushButton {{
                background-color: {"#238636" if dark else "#1f883d"}; color: white;
                border: none; border-radius: 6px; padding: 5px 14px; min-width: 60px;
            }}
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
                if value.lower() in {"host", "hostname", "ip", "ip address", "address", "host/ip"}:
                    return

                for segment in re.split(r"[,;\t]+", value):
                    segment = segment.strip().strip("\"'")
                    paren = re.search(r"\(([^()]+)\)", segment)
                    if paren:
                        segment = paren.group(1).strip()
                    for candidate in re.split(r"\s+", segment):
                        candidate = candidate.strip().strip("\"'")
                        if not candidate or candidate.lower() in {"host", "hostname", "ip", "address"}:
                            continue
                        normalized = candidate.casefold()
                        if normalized not in seen:
                            seen.add(normalized)
                            hosts.append(candidate)

            if file_path.lower().endswith(".csv"):
                with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                    rows = list(csv.reader(f))
                if rows:
                    headers = [c.strip().lower() for c in rows[0]]
                    ip_cols = [
                        i
                        for i, h in enumerate(headers)
                        if h in {"ip", "ip address", "ip_address", "address", "host", "hostname", "host/ip"}
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
                self._msg("No Hosts Found", "No usable hosts found in file.", QMessageBox.Warning)
                return

            current = [h.strip() for h in self.host_entry.text().split(",") if h.strip()]
            merged = []
            seen_merged = set()
            for host in current + hosts:
                if host not in seen_merged:
                    seen_merged.add(host)
                    merged.append(host)
            self.host_entry.setText(", ".join(merged))
            self._msg("Hosts Imported", f"Successfully imported {len(hosts)} host(s).")
        except Exception as e:
            self._msg("Import Error", f"Failed to import file:\n{e}", QMessageBox.Critical)

    def save_config(self):
        config = {
            "hosts": self.host_entry.text(),
            "packet_size": self.packet_spin.value(),
            "interval": self.interval_spin.value(),
            "timeout": self.timeout_spin.value(),
            "history_points": self.history_spin.value(),
            "dark_theme": self._is_dark,
        }
        config_path = Path(self.CONFIG_FILE)
        temporary_path = None
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=config_path.parent,
                prefix=f".{config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as config_file:
                json.dump(config, config_file, indent=2)
                config_file.flush()
                os.fsync(config_file.fileno())
                temporary_path = Path(config_file.name)
            os.replace(temporary_path, config_path)
            temporary_path = None
            self._msg("Config Saved", f"Settings saved to {config_path}")
        except (OSError, TypeError, ValueError) as error:
            self._msg("Error", f"Failed to save config:\n{error}", QMessageBox.Critical)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def load_config(self):
        config_path = Path(self.CONFIG_FILE)
        if not config_path.exists():
            return
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("The config root must be a JSON object.")

            hosts = config.get("hosts")
            if hosts is not None:
                if not isinstance(hosts, str):
                    raise ValueError("The 'hosts' setting must be text.")
            pending_values = {}
            for key, control, integer_only in (
                ("packet_size", self.packet_spin, True),
                ("interval", self.interval_spin, False),
                ("timeout", self.timeout_spin, False),
                ("history_points", self.history_spin, True),
            ):
                if key not in config:
                    continue
                value = config[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"The '{key}' setting must be numeric.")
                if integer_only and not isinstance(value, int):
                    raise ValueError(f"The '{key}' setting must be an integer.")
                if not math.isfinite(value):
                    raise ValueError(f"The '{key}' setting must be finite.")
                minimum = control.minimum()
                maximum = control.maximum()
                if value < minimum or value > maximum:
                    raise ValueError(
                        f"The '{key}' setting must be between {minimum} and {maximum}."
                    )
                pending_values[key] = value

            dark_theme = self._is_dark
            if "dark_theme" in config:
                if not isinstance(config["dark_theme"], bool):
                    raise ValueError("The 'dark_theme' setting must be true or false.")
                dark_theme = config["dark_theme"]

            # Apply only after every field passes validation, so malformed files
            # leave the complete set of defaults intact.
            if hosts is not None:
                self.host_entry.setText(hosts)
            for key, value in pending_values.items():
                {
                    "packet_size": self.packet_spin,
                    "interval": self.interval_spin,
                    "timeout": self.timeout_spin,
                    "history_points": self.history_spin,
                }[key].setValue(value)
            self._is_dark = dark_theme

            self.apply_style()
            self.chart.set_dark(self._is_dark)
            self.chart.update()
        except (OSError, json.JSONDecodeError, OverflowError, TypeError, ValueError) as error:
            self.config_warning = str(error)

    # ------------------------------------------------------------------ Monitoring
    def toggle_monitoring(self):
        if self.is_monitoring:
            self.stop_monitoring()
            return

        try:
            self.hosts, duplicate_count = normalize_targets(self.host_entry.text())
        except ValueError as error:
            self._msg("Invalid Targets", str(error), QMessageBox.Warning)
            return

        if not self.hosts:
            self._msg("No Hosts", "Please enter at least one target host.", QMessageBox.Warning)
            return

        self.host_entry.setText(", ".join(self.hosts))
        if duplicate_count:
            self._msg(
                "Duplicate Targets Removed",
                f"Monitoring each target once; removed {duplicate_count} duplicate entr{'y' if duplicate_count == 1 else 'ies'}.",
                QMessageBox.Information,
            )

        self.packet_size_bytes = self.packet_spin.value()
        self.ping_interval_seconds = self.interval_spin.value()
        self.graph_history_points = self.history_spin.value()
        self.timeout_seconds = self.timeout_spin.value()

        self.host_data.clear()
        self._host_items.clear()
        self._last_down_signature = None
        self.table.setRowCount(0)
        self.down_table.setRowCount(0)

        self._run_id += 1
        for host in self.hosts:
            self.host_data[host] = {
                "latencies": deque(maxlen=self.graph_history_points),
                "latency_count": 0,
                "latency_sum": 0.0,
                "min_latency": None,
                "max_latency": None,
                "sent": 0,
                "received": 0,
                "timeline_stats": [],
                "log_rows": [],
                "tree_id": host,
                "last_status": None,
                "last_error": None,
                "consecutive_fails": 0,
                "last_down_time": "-",
            }
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [host, "Queued", "0", "-", "0%", "0"]
            dark = self._is_dark
            text_color = QColor("#e6edf3" if dark else "#1f2328")
            bg_color = QColor("#0d1117" if dark else "#ffffff")
            for col, value in enumerate(values):
                item = SortableTableWidgetItem(str(value), numeric=col in (2, 3, 4, 5))
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(text_color)
                item.setBackground(bg_color)
                self.table.setItem(row, col, item)
                if col == 0:
                    self._host_items[host] = item

        self.is_monitoring = True
        self._stop_pending = False
        self.set_controls_enabled(False)
        self.start_btn.setText("Stop Monitoring")
        self._set_status_running()
        self.export_txt_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        task = ProbeTask(
            run_id=self._run_id,
            targets=self.hosts,
            payload_size=self.packet_size_bytes,
            timeout=self.timeout_seconds,
            interval=self.ping_interval_seconds,
        )
        task.signals.completed.connect(self._handle_probe_result)
        task.signals.finished.connect(self._handle_probe_task_finished)
        self.probe_task = task
        self.probe_pool.start(task)

    def stop_monitoring(self):
        if not self.is_monitoring:
            return
        self.is_monitoring = False
        self._stop_pending = True
        if self.probe_task is not None:
            self.probe_task.request_stop()
            self.set_controls_enabled(False)
            self.start_btn.setText("Stopping…")
            self.start_btn.setEnabled(False)
            self.status_pill.setText("STOPPING")
            self.footer_label.setText("Finishing current probes…")
        else:
            self._finalize_stop()

    def _finalize_stop(self):
        self._stop_pending = False
        self.probe_task = None
        self.set_controls_enabled(True)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Start Monitoring")
        self._set_status_idle()
        self.export_txt_btn.setEnabled(bool(self.host_data))
        self.export_csv_btn.setEnabled(bool(self.host_data))
        self.clear_btn.setEnabled(bool(self.host_data))
        self.update_footer_stats()
        if self.host_data and self._confirm(
            "Export Report", "Monitoring stopped. Export performance report summary?"
        ):
            self.export_report_summary()

    @Slot(int)
    def _handle_probe_task_finished(self, run_id):
        if run_id != self._run_id:
            return
        self.probe_task = None
        if self._stop_pending:
            self._finalize_stop()
        elif self.is_monitoring:
            self.is_monitoring = False
            self._finalize_stop()
            self._msg(
                "Probe Runner Stopped",
                "The background probe runner stopped unexpectedly. You can start monitoring again.",
                QMessageBox.Warning,
            )

    @Slot(int, str, object)
    def _handle_probe_result(self, run_id, target, result):
        if run_id != self._run_id or not self.is_monitoring:
            return

        if target not in self.host_data:
            return
        if not isinstance(result, ProbeResult):
            result = ProbeResult("Probe Error", error="The probe returned an invalid result.")

        data = self.host_data[target]
        valid_statuses = {
            "Connected", "Timed Out", "DNS Error", "Permission Denied", "Probe Error", "Unreachable"
        }
        if result.status not in valid_statuses:
            result = ProbeResult("Probe Error", error=f"Unknown probe status: {result.status!r}")
        elif result.status == "Connected" and (
            result.latency_ms is None
            or not isinstance(result.latency_ms, (int, float))
            or not math.isfinite(result.latency_ms)
            or result.latency_ms < 0
        ):
            result = ProbeResult("Probe Error", error="The probe returned an invalid latency value.")
        data["sent"] += 1
        sequence = data["sent"]
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        status = result.status
        data["last_status"] = status
        data["last_error"] = result.error

        if status == "Connected" and result.latency_ms is not None:
            latency = result.latency_ms
            data["received"] += 1
            data["latencies"].append(latency)
            data["latency_count"] += 1
            data["latency_sum"] += latency
            data["min_latency"] = latency if data["min_latency"] is None else min(data["min_latency"], latency)
            data["max_latency"] = latency if data["max_latency"] is None else max(data["max_latency"], latency)
            data["consecutive_fails"] = 0
            latency_text = f"{latency:.2f} ms"
            timeline = f"[{timestamp}] Seq {sequence}: Success — Latency {latency:.2f} ms"
            log_result = (sequence, timestamp, "Success", latency)
        else:
            data["latencies"].append(0)
            data["consecutive_fails"] += 1
            data["last_down_time"] = timestamp
            latency_text = "—"
            error_text = f" — {result.error}" if result.error else ""
            timeline = f"[{timestamp}] Seq {sequence}: FAILED — {status}{error_text}"
            log_result = (sequence, timestamp, status, result.error or "")

        data["timeline_stats"].append(timeline)
        data["log_rows"].append(log_result)
        del data["timeline_stats"][:-self.MAX_LOG_ENTRIES]
        del data["log_rows"][:-self.MAX_LOG_ENTRIES]

        loss_pct = round(((data["sent"] - data["received"]) / data["sent"]) * 100, 1)
        row_values = (
            target,
            status,
            str(sequence),
            latency_text,
            f"{loss_pct}%",
            str(data["consecutive_fails"]),
        )
        self.update_row(target, row_values)
        self.update_footer_stats()

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
        text_color = QColor("#e6edf3" if dark else "#1f2328")
        bg_color = QColor("#0d1117" if dark else "#ffffff")
        for col, value in enumerate(values):
            item = SortableTableWidgetItem(str(value), numeric=col in (2, 3, 4, 5))
            item.setTextAlignment(Qt.AlignCenter)
            item.setForeground(text_color)
            item.setBackground(bg_color)
            self.table.setItem(row, col, item)
            if col == 0:
                self._host_items[host] = item
        status = str(values[1])
        status_item = self.table.item(row, 1)
        if status == "Connected":
            status_item.setForeground(QColor("#3fb950" if dark else "#1a7f37"))
        elif status == "Timed Out":
            status_item.setForeground(QColor("#d29922" if dark else "#9a6700"))
        else:
            status_item.setForeground(QColor("#f85149" if dark else "#cf222e"))
        status_item.setToolTip(self.host_data.get(host, {}).get("last_error") or "")
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
        if self.is_monitoring or not self.host_data:
            return
        if not self._confirm("Clear History", "Remove all monitoring data and history?"):
            return
        self.host_data.clear()
        self._host_items.clear()
        self._last_down_signature = None
        self.table.setRowCount(0)
        self.down_table.setRowCount(0)
        self.host_count_label.setText("0 hosts")
        self.down_summary_label.setText("No targets monitored")
        dark = self._is_dark
        self.down_summary_label.setStyleSheet(
            f"color: {'#3fb950' if dark else '#1a7f37'}; font-weight: 600; background: transparent;"
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
        pending_count = sum(1 for data in self.host_data.values() if data.get("sent", 0) == 0)
        down_hosts = [
            (host, data)
            for host, data in self.host_data.items()
            if self._is_down_status(data.get("last_status"))
        ]
        signature = (
            tuple(
                (
                    host,
                    data.get("last_status"),
                    data.get("consecutive_fails", 0),
                    data.get("last_down_time", "-"),
                )
                for host, data in down_hosts
            ),
            pending_count,
        )
        if signature != self._last_down_signature:
            self._last_down_signature = signature
            self.down_table.setRowCount(0)
            dark = self._is_dark
            if down_hosts and pending_count:
                self.down_summary_label.setText(
                    f"{len(down_hosts)} degraded/down · {pending_count} awaiting first result"
                )
                self.down_summary_label.setStyleSheet(
                    "color: #f85149; font-weight: 600; background: transparent;"
                )
            elif pending_count:
                self.down_summary_label.setText(f"{pending_count} target(s) awaiting first result")
                muted = "#848d97" if dark else "#656d76"
                self.down_summary_label.setStyleSheet(
                    f"color: {muted}; font-weight: 600; background: transparent;"
                )
            elif not down_hosts:
                self.down_summary_label.setText("All hosts operational")
                self.down_summary_label.setStyleSheet(
                    f"color: {'#3fb950' if dark else '#1a7f37'}; font-weight: 600; background: transparent;"
                )
            else:
                self.down_summary_label.setText(f"{len(down_hosts)} host(s) degraded or down")
                self.down_summary_label.setStyleSheet(
                    "color: #f85149; font-weight: 600; background: transparent;"
                )
            if down_hosts:
                text_color = QColor("#e6edf3" if dark else "#1f2328")
                bg_color = QColor("#0d1117" if dark else "#ffffff")
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
                        item = SortableTableWidgetItem(str(value), numeric=col in (2, 3))
                        item.setTextAlignment(Qt.AlignCenter)
                        item.setForeground(text_color)
                        item.setBackground(bg_color)
                        self.down_table.setItem(row, col, item)
        self.update_footer_stats()

    def update_footer_stats(self):
        if not self.host_data:
            self.footer_label.setText("No targets yet • Ready")
            return
        total_sent = sum(d["sent"] for d in self.host_data.values())
        total_recv = sum(d["received"] for d in self.host_data.values())
        overall_loss = ((total_sent - total_recv) / total_sent * 100) if total_sent else 0.0
        state = "Waiting for first results" if self.is_monitoring and not total_sent else (
            "Monitoring" if self.is_monitoring else "Stopped"
        )
        self.footer_label.setText(
            f"{len(self.host_data)} targets  |  Aggregate loss: {overall_loss:.1f}%  |  {state}"
        )

    # ------------------------------------------------------------------ Export
    def export_report_summary(self):
        if not self.host_data:
            self._msg("No Data", "No monitoring data available for export.", QMessageBox.Warning)
            return

        file_path = self._open_file_dialog(
            "Export Summary Report",
            "Text Files (*.txt);;All Files (*)",
            save=True,
            default_name="network_report.txt",
            default_suffix="txt",
        )
        if not file_path:
            return

        data_snapshot = {host: dict(data) for host, data in self.host_data.items()}

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("=" * 70 + "\n")
                f.write("             NETWORK PERFORMANCE SUMMARY REPORT\n")
                f.write(f"             Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 70 + "\n\n")

                f.write("[1. HOST AGGREGATE METRICS]\n")
                f.write("-" * 80 + "\n")
                f.write(
                    f"{'Target Host':<20} | {'Sent':<6} | {'Recv':<6} | {'Loss %':<8} | "
                    f"{'Min':<9} | {'Max':<9} | {'Avg':<9}\n"
                )
                f.write("-" * 80 + "\n")

                for host, data in data_snapshot.items():
                    sent, received, loss_pct, min_lat, max_lat, avg_lat = self._compute_host_stats(data)
                    if min_lat is None:
                        min_lat, max_lat, avg_lat = "-", "-", "-"
                    else:
                        min_lat, max_lat, avg_lat = f"{min_lat}ms", f"{max_lat}ms", f"{avg_lat}ms"
                    f.write(
                        f"{host:<20} | {sent:<6} | {received:<6} | {str(loss_pct) + '%':<8} | "
                        f"{min_lat:<9} | {max_lat:<9} | {avg_lat:<9}\n"
                    )
                f.write("-" * 80 + "\n\n")

                f.write("[2. DEGRADED / DOWN HOSTS AT EXPORT]\n")
                f.write("-" * 50 + "\n")
                down_now = [
                    h for h, d in data_snapshot.items() if self._is_down_status(d.get("last_status"))
                ]
                if down_now:
                    for h in down_now:
                        f.write(
                            f"• {h} (last failed: {data_snapshot[h].get('last_down_time', '-')})\n"
                        )
                else:
                    f.write("All hosts operational.\n")
                f.write("\n")

                f.write("[3. DETAILED SEQUENCE TIMELINES]\n")
                for host, data in data_snapshot.items():
                    f.write(f"\n# Stream: {host}\n")
                    f.write("-" * 50 + "\n")
                    for log_entry in data["timeline_stats"]:
                        f.write(log_entry + "\n")

            self._msg("Success", f"Report successfully exported to:\n{file_path}")
        except Exception as e:
            self._msg("Error", f"Failed to write report:\n{str(e)}", QMessageBox.Critical)

    def export_report_csv(self):
        if not self.host_data:
            self._msg("No Data", "No monitoring data available for export.", QMessageBox.Warning)
            return

        file_path = self._open_file_dialog(
            "Export CSV Report",
            "CSV Files (*.csv);;All Files (*)",
            save=True,
            default_name="network_report.csv",
            default_suffix="csv",
        )
        if not file_path:
            return

        data_snapshot = {host: dict(data) for host, data in self.host_data.items()}

        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                fieldnames = [
                    "Section", "Host", "Sequence", "Status", "Sent", "Received",
                    "Loss_Pct", "Latency_ms", "Min_ms", "Max_ms", "Avg_ms",
                    "Consecutive_Fails", "Last_Down_At", "Timestamp", "Details",
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                generated = time.strftime("%Y-%m-%d %H:%M:%S")
                for host, data in data_snapshot.items():
                    sent, received, loss_pct, min_lat, max_lat, avg_lat = self._compute_host_stats(data)
                    writer.writerow({
                        "Section": "Summary",
                        "Host": host,
                        "Status": data.get("last_status") or "-",
                        "Sent": sent,
                        "Received": received,
                        "Loss_Pct": loss_pct,
                        "Min_ms": min_lat if min_lat is not None else "",
                        "Max_ms": max_lat if max_lat is not None else "",
                        "Avg_ms": avg_lat if avg_lat is not None else "",
                        "Consecutive_Fails": data.get("consecutive_fails", 0),
                        "Last_Down_At": data.get("last_down_time", "-"),
                        "Timestamp": generated,
                    })
                    if self._is_down_status(data.get("last_status")):
                        writer.writerow({
                            "Section": "Down",
                            "Host": host,
                            "Status": data.get("last_status"),
                            "Consecutive_Fails": data.get("consecutive_fails", 0),
                            "Last_Down_At": data.get("last_down_time", "-"),
                            "Details": data.get("last_error") or "",
                        })
                    for sequence, timestamp, status, details in data.get("log_rows", []):
                        writer.writerow({
                            "Section": "Probe",
                            "Host": host,
                            "Sequence": sequence,
                            "Status": status,
                            "Latency_ms": details if status == "Success" else "",
                            "Timestamp": timestamp,
                            "Details": "" if status == "Success" else details,
                        })
            self._msg("Success", f"CSV successfully exported to:\n{file_path}")
        except Exception as e:
            self._msg("Error", f"Failed to write CSV:\n{str(e)}", QMessageBox.Critical)

    def closeEvent(self, event):
        self.is_monitoring = False
        self._run_id += 1
        self.timer.stop()
        if self.probe_task is not None:
            self.probe_task.request_stop()
            self.probe_task = None
        self.probe_pool.clear()
        self.probe_pool.waitForDone(int((self.timeout_seconds + 2) * 1000))
        event.accept()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Ping Check Enhanced")

    window = PingMonitorApp()
    window.show()
    if window.config_warning:
        QTimer.singleShot(
            0,
            lambda warning=window.config_warning: window._msg(
                "Config Not Loaded", f"Using defaults because the config could not be loaded:\n{warning}",
                QMessageBox.Warning,
            ),
        )
    try:
        exit_code = app.exec()
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
