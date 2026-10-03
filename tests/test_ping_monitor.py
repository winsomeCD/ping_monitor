import csv
import json
import os
import re
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from openpyxl import load_workbook

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer, Qt
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

    def test_invalid_config_is_rejected_without_partially_applying_values(self):
        self.window.host_entry.setText("keep.example")
        Path(self.window.CONFIG_FILE).write_text(
            '{"hosts": "replace.example", "packet_size": 1200, "interval": 99999}',
            encoding="utf-8",
        )

        self.window.load_config()

        self.assertIn("interval", self.window.config_warning)
        self.assertEqual(self.window.host_entry.text(), "keep.example")
        self.assertEqual(self.window.packet_spin.value(), 56)

    def test_stop_keeps_controls_disabled_until_worker_finishes(self):
        class PendingTask:
            def __init__(self):
                self.stop_requested = False

            def request_stop(self):
                self.stop_requested = True

        run_id = 17
        task = PendingTask()
        self.window._run_id = run_id
        self.window.is_monitoring = True
        self.window.probe_task = task

        self.window.stop_monitoring()

        self.assertTrue(task.stop_requested)
        self.assertFalse(self.window.host_entry.isEnabled())
        self.assertEqual(self.window.status_pill.text(), "STOPPING")

        self.window._handle_probe_task_finished(run_id)

        self.assertTrue(self.window.host_entry.isEnabled())
        self.assertEqual(self.window.status_pill.text(), "IDLE")

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

    def test_supplied_500_host_list_imports_without_changing_targets(self):
        host_file = PROJECT_DIR / "ping_domains.txt"
        self.window.host_entry.clear()
        self.window._open_file_dialog = lambda *args, **kwargs: str(host_file)

        self.window.import_hosts_from_file()

        imported = self.window.host_entry.text().split(", ")
        self.assertEqual(len(imported), 500)
        self.assertEqual(imported[0], "google.com")
        self.assertIn("hotels.com", imported)

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
            "last_status": "No ICMP Reply",
            "last_error": "name lookup failed",
            "consecutive_fails": 1,
            "last_down_time": "2026-10-03 12:00:00",
            "log_rows": [(1, "2026-10-03 12:00:00", "No ICMP Reply", "")],
        }
        report_path = Path(self.temporary_directory.name) / "report.csv"
        self.window._open_file_dialog = lambda *args, **kwargs: str(report_path)

        self.window.export_report_csv()

        with report_path.open(encoding="utf-8-sig", newline="") as report_file:
            rows = list(csv.DictReader(report_file))
        sections = [row["Section"] for row in rows if row["Section"]]
        self.assertEqual(sections, ["Summary", "No ICMP Reply", "Probe"])
        no_reply_row = next(row for row in rows if row["Section"] == "No ICMP Reply")
        self.assertEqual(no_reply_row["Status"], "No ICMP Reply")

    def test_xlsx_export_contains_summary_and_detail_sheets(self):
        target = "router.example"
        self.window.host_data[target] = {
            "sent": 2, "received": 1, "checks": 3,
            "latencies": deque([5.0, 9.0], maxlen=60), "latency_count": 2,
            "latency_sum": 14.0, "min_latency": 5.0, "max_latency": 9.0,
            "last_status": "No ICMP Reply", "last_error": None,
            "consecutive_fails": 1, "last_down_time": "2026-10-03 12:00:00",
            "log_rows": [(1, "12:00:00", "Success", 5.0),
                         (2, "12:00:01", "Success", 9.0),
                         (3, "12:00:02", "No ICMP Reply", "")],
        }
        report_path = Path(self.temporary_directory.name) / "report.xlsx"
        self.window._open_file_dialog = lambda *args, **kwargs: str(report_path)
        messages = []
        self.window._msg = lambda *args: messages.append(args)

        self.window.export_report_xlsx()

        self.assertTrue(report_path.is_file(), messages)
        workbook = load_workbook(report_path, data_only=True)
        self.assertEqual(workbook.sheetnames, ["Summary Info", "Host Details", "Probe Details"])
        summary = {row[0]: row[1] for row in workbook["Summary Info"].iter_rows(min_row=5, values_only=True)}
        self.assertEqual(summary["Total targets"], 1)
        self.assertEqual(summary["Targets with no ICMP reply"], 1)
        self.assertEqual(summary["ICMP requests sent"], 2)
        self.assertEqual(summary["ICMP replies received"], 1)
        self.assertEqual(summary["ICMP packet loss (%)"], 50.0)
        self.assertEqual(workbook["Host Details"].max_row, 2)
        self.assertEqual(workbook["Probe Details"].max_row, 4)

    def test_txt_export_starts_with_summary_overview(self):
        target = "router.example"
        self.window.host_data[target] = {
            "sent": 2, "received": 1, "checks": 3,
            "latencies": deque([5.0, 9.0], maxlen=60), "latency_count": 2,
            "latency_sum": 14.0, "min_latency": 5.0, "max_latency": 9.0,
            "last_status": "No ICMP Reply", "last_error": None,
            "consecutive_fails": 1, "last_down_time": "2026-10-03 12:00:00",
            "timeline_stats": ["Seq 1: Connected"], "log_rows": [],
        }
        report_path = Path(self.temporary_directory.name) / "report.txt"
        self.window._open_file_dialog = lambda *args, **kwargs: str(report_path)

        self.window.export_report_summary()

        report = report_path.read_text(encoding="utf-8")
        self.assertLess(report.index("[1. SUMMARY INFO]"), report.index("[2. HOST AGGREGATE METRICS]"))
        self.assertIn("Total targets", report)
        self.assertIn("Targets with no ICMP reply", report)
        self.assertIn("ICMP packet loss (%)", report)

    def test_export_default_filenames_include_timestamp(self):
        target = "router.example"
        self.window.host_data[target] = {
            "sent": 1, "received": 1, "checks": 1,
            "latencies": deque([5.0], maxlen=60), "latency_count": 1,
            "latency_sum": 5.0, "min_latency": 5.0, "max_latency": 5.0,
            "last_status": "Connected", "last_error": None,
            "consecutive_fails": 0, "last_down_time": "-",
            "timeline_stats": ["Seq 1: Connected"], "log_rows": [(1, "12:00:00", "Success", 5.0)],
        }
        captured_defaults = {}

        def fake_dialog(title, name_filter, save=False, default_name="", default_suffix=""):
            captured_defaults[default_suffix] = default_name
            return None

        self.window._open_file_dialog = fake_dialog

        self.window.export_report_summary()
        self.window.export_report_xlsx()
        self.window.export_report_csv()

        self.assertTrue(re.match(r"^network_report_\d{8}_\d{6}\.txt$", captured_defaults.get("txt", "")))
        self.assertTrue(re.match(r"^network_report_\d{8}_\d{6}\.xlsx$", captured_defaults.get("xlsx", "")))
        self.assertTrue(re.match(r"^network_report_\d{8}_\d{6}\.csv$", captured_defaults.get("csv", "")))

    def test_dns_errors_do_not_count_as_sent_packets_or_host_downs(self):
        target = "nonexistent.example"
        self.window._run_id = 3
        self.window.is_monitoring = True
        self.window.host_data[target] = {
            "sent": 0, "received": 0, "checks": 0,
            "latencies": deque(maxlen=60), "latency_count": 0,
            "latency_sum": 0.0, "min_latency": None, "max_latency": None,
            "timeline_stats": [], "log_rows": [], "last_status": None,
            "last_error": None, "consecutive_fails": 0, "last_down_time": "-",
        }
        self.window.table.setRowCount(1)
        host_item = QTableWidgetItem(target)
        self.window.table.setItem(0, 0, host_item)
        self.window._host_items[target] = host_item

        self.window._handle_probe_result(
            3, target, ProbeResult("DNS Error", error="lookup failed", packet_sent=False)
        )

        self.assertEqual(self.window.host_data[target]["sent"], 0)
        self.assertEqual(self.window.host_data[target]["checks"], 1)
        self.assertFalse(self.window._is_down_status("DNS Error"))
        self.window.refresh_dashboard()
        self.assertIn("DNS/probe issue", self.window.down_summary_label.text())
        self.assertNotIn("awaiting first result", self.window.down_summary_label.text())
        self.assertEqual(self.window.host_count_label.text(), "0 up / 1 total")

    def test_no_reply_count_matches_sorted_table_and_other_issue_counts(self):
        def host_data(status, received=0):
            return {
                "sent": 1, "received": received, "checks": 1,
                "latencies": deque(maxlen=60), "latency_count": 0,
                "latency_sum": 0.0, "min_latency": None, "max_latency": None,
                "timeline_stats": [], "log_rows": [], "last_status": status,
                "last_error": None, "consecutive_fails": 1 if status != "Connected" else 0,
                "last_down_time": "2026-10-03 12:00:00" if status == "No ICMP Reply" else "-",
            }

        self.window.host_data = {
            "z-down.example": host_data("No ICMP Reply"),
            "a-down.example": host_data("No ICMP Reply"),
            "b-dns.example": host_data("DNS Error"),
            "c-up.example": host_data("Connected", received=1),
        }
        self.window.down_table.sortItems(0, Qt.DescendingOrder)

        self.window.refresh_dashboard()

        self.assertEqual(self.window.down_table.rowCount(), 2)
        self.assertIn("2 of 4 down", self.window.down_summary_label.text())
        self.assertIn("1 DNS/probe issue", self.window.down_summary_label.text())
        self.assertEqual(self.window.host_count_label.text(), "1 up / 4 total")
        displayed = {
            self.window.down_table.item(row, 0).text(): self.window.down_table.item(row, 1).text()
            for row in range(self.window.down_table.rowCount())
        }
        self.assertEqual(set(displayed), {"z-down.example", "a-down.example"})
        self.assertTrue(all(status == "No ICMP Reply" for status in displayed.values()))

        self.window.host_data["z-down.example"]["last_status"] = "Connected"
        self.window.refresh_dashboard()

        self.assertEqual(self.window.down_table.rowCount(), 1)
        self.assertIn("1 of 4 down", self.window.down_summary_label.text())
        self.assertEqual(self.window.host_count_label.text(), "2 up / 4 total")


if __name__ == "__main__":
    unittest.main()
