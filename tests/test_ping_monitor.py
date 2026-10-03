import csv
import json
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QTableWidgetItem

import ping_monitor
from probes import ProbeResult


class PingMonitorAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_config_path = ping_monitor.PingMonitorApp.CONFIG_FILE
        ping_monitor.PingMonitorApp.CONFIG_FILE = Path(self.temporary_directory.name) / "config.json"
        self.window = ping_monitor.PingMonitorApp()
        self.window._msg = lambda *args: None
        self.window._confirm = lambda *args: False

    def tearDown(self):
        self.window.close()
        self.application.processEvents()
        ping_monitor.PingMonitorApp.CONFIG_FILE = self.original_config_path
        self.temporary_directory.cleanup()

    def test_theme_is_saved_and_restored(self):
        self.assertEqual(self.window.theme_btn.text(), "☾")
        self.window.toggle_theme()
        self.assertEqual(self.window.theme_btn.text(), "☀")
        self.window.save_config()

        saved_config = json.loads(Path(self.window.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertIs(saved_config["dark_theme"], False)

        restored_window = ping_monitor.PingMonitorApp()
        self.assertFalse(restored_window._is_dark)
        self.assertEqual(restored_window.theme_btn.text(), "☀")
        restored_window.close()

    def test_invalid_config_is_reported(self):
        Path(self.window.CONFIG_FILE).write_text('{"interval": "fast"}', encoding="utf-8")

        self.window.load_config()

        self.assertIn("interval", self.window.config_warning)

    def test_import_hosts_reads_all_targets_from_one_comma_separated_line(self):
        host_file = Path(self.temporary_directory.name) / "hosts.txt"
        host_file.write_text(
            "one.example, two.example; Router (192.0.2.10), 192.0.2.11\n",
            encoding="utf-8",
        )
        self.window.host_entry.clear()
        self.window._open_file_dialog = lambda *args, **kwargs: str(host_file)

        self.window.import_hosts_from_file()

        self.assertEqual(
            self.window.host_entry.text(),
            "one.example, two.example, 192.0.2.10, 192.0.2.11",
        )

    def test_result_updates_are_main_thread_owned_and_history_is_bounded(self):
        target = "localhost"
        self.window._run_id = 4
        self.window.is_monitoring = True
        self.window.graph_history_points = 2
        self.window.host_data[target] = {
            "latencies": deque(maxlen=2),
            "latency_count": 0,
            "latency_sum": 0.0,
            "min_latency": None,
            "max_latency": None,
            "sent": 0,
            "received": 0,
            "timeline_stats": [],
            "log_rows": [],
            "last_status": None,
            "last_error": None,
            "consecutive_fails": 0,
            "last_down_time": "-",
        }
        self.window.table.setRowCount(1)
        host_item = QTableWidgetItem(target)
        self.window.table.setItem(0, 0, host_item)
        self.window._host_items[target] = host_item

        for latency in (10.0, 20.0, 30.0):
            self.window._handle_probe_result(4, target, ProbeResult("Connected", latency))

        data = self.window.host_data[target]
        self.assertEqual(list(data["latencies"]), [20.0, 30.0])
        self.assertEqual(ping_monitor.PingMonitorApp._compute_host_stats(data)[3:], (10.0, 30.0, 20.0))

        self.window._handle_probe_result(3, target, ProbeResult("Connected", 40.0))
        self.assertEqual(data["sent"], 3)

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_thread_pool_probe_updates_the_window(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=True, avg_rtt=0.5)
        self.window.host_entry.setText("127.0.0.1")
        self.window.timeout_spin.setValue(0.5)
        self.window.ping_interval_seconds = 1.0
        self.window.toggle_monitoring()

        event_loop = QEventLoop()
        QTimer.singleShot(500, event_loop.quit)
        event_loop.exec()
        self.window.stop_monitoring()

        data = self.window.host_data["127.0.0.1"]
        self.assertEqual(data["sent"], 1)
        self.assertEqual(data["received"], 1)
        self.assertEqual(self.window.table.item(0, 1).text(), "Connected")
        ping_mock.assert_awaited_once()

    def test_csv_export_includes_summary_down_and_probe_rows(self):
        target = "invalid.example"
        self.window.host_data[target] = {
            "sent": 1,
            "received": 0,
            "latencies": deque(maxlen=60),
            "latency_count": 0,
            "latency_sum": 0.0,
            "min_latency": None,
            "max_latency": None,
            "last_status": "DNS Error",
            "last_error": "name lookup failed",
            "consecutive_fails": 1,
            "last_down_time": "2026-10-03 12:00:00",
            "log_rows": [(1, "2026-10-03 12:00:00", "DNS Error", "name lookup failed")],
        }
        report_path = Path(self.temporary_directory.name) / "report.csv"
        self.window._open_file_dialog = lambda *args, **kwargs: str(report_path)

        self.window.export_report_csv()

        with report_path.open(encoding="utf-8-sig", newline="") as report_file:
            rows = list(csv.DictReader(report_file))
        self.assertEqual([row["Section"] for row in rows], ["Summary", "Down", "Probe"])
        self.assertEqual(rows[2]["Details"], "name lookup failed")


if __name__ == "__main__":
    unittest.main()