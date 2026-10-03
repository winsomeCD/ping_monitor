import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from icmplib import NameLookupError, SocketPermissionError

from probes import ProbeResult, ProbeTask, normalize_targets, probe_target


class TargetValidationTests(unittest.TestCase):
    def test_normalize_targets_deduplicates_case_insensitively(self):
        targets, duplicate_count = normalize_targets("router.local, ROUTER.LOCAL, 1.1.1.1")

        self.assertEqual(targets, ["router.local", "1.1.1.1"])
        self.assertEqual(duplicate_count, 1)

    def test_normalize_targets_rejects_embedded_whitespace(self):
        with self.assertRaisesRegex(ValueError, "whitespace"):
            normalize_targets("router one.local")

    def test_normalize_targets_accepts_more_than_256_targets(self):
        raw_targets = ",".join(f"host-{index}.local" for index in range(1024))

        targets, duplicate_count = normalize_targets(raw_targets)

        self.assertEqual(len(targets), 1024)
        self.assertEqual(duplicate_count, 0)


class ProbeTests(unittest.TestCase):
    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_target_returns_latency(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=True, avg_rtt=4.126)
        result = asyncio.run(probe_target("router.local", payload_size=56, timeout=1.5))

        self.assertEqual(result, ProbeResult("Connected", 4.13))
        ping_mock.assert_awaited_once_with(
            "router.local", count=1, timeout=1.5, privileged=False, payload_size=56
        )

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_target_maps_no_reply_to_timeout(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=False, avg_rtt=0)
        result = asyncio.run(probe_target("192.0.2.1", 56, 0.2))
        self.assertEqual(result, ProbeResult("Timed Out"))

    @patch("probes.async_ping", new_callable=AsyncMock, side_effect=NameLookupError("bad host"))
    def test_probe_target_maps_dns_errors(self, _ping_mock):
        result = asyncio.run(probe_target("bad host", 56, 0.2))
        self.assertEqual(result.status, "DNS Error")

    @patch("probes.async_ping", new_callable=AsyncMock, side_effect=SocketPermissionError("permission denied"))
    def test_probe_target_maps_permission_errors(self, _ping_mock):
        result = asyncio.run(probe_target("router.local", 56, 0.2))
        self.assertEqual(result.status, "Permission Denied")

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_task_emits_a_result_and_stops(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=True, avg_rtt=1.25)
        task = ProbeTask(8, ["localhost"], 56, 1.0, 0.1)
        emitted = []
        task.signals.completed.connect(lambda *values: (emitted.append(values), task.request_stop()))

        task.run()

        self.assertEqual(emitted, [(8, "localhost", ProbeResult("Connected", 1.25))])
        self.assertEqual(ping_mock.await_count, 1)

    @patch("probes.async_ping", new_callable=AsyncMock, side_effect=RuntimeError("unexpected failure"))
    def test_probe_task_converts_unexpected_errors(self, _ping_mock):
        task = ProbeTask(2, ["localhost"], 56, 1.0, 0.1)
        emitted = []
        task.signals.completed.connect(lambda *values: (emitted.append(values), task.request_stop()))

        task.run()

        self.assertEqual(emitted[0][0:2], (2, "localhost"))
        self.assertEqual(emitted[0][2].status, "Probe Error")
        self.assertIn("unexpected failure", emitted[0][2].error)

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_task_runs_all_targets_concurrently(self, ping_mock):
        active = 0
        peak_active = 0

        async def fake_ping(*_args, **_kwargs):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return SimpleNamespace(is_alive=True, avg_rtt=1.0)

        ping_mock.side_effect = fake_ping
        targets = [f"host-{index}.example" for index in range(1000)]
        task = ProbeTask(9, targets, 56, 1.0, 1.0)
        completed = []

        def on_completed(*values):
            completed.append(values)
            if len(completed) == len(targets):
                task.request_stop()

        task.signals.completed.connect(on_completed)
        task.run()

        self.assertEqual(len(completed), 1000)
        self.assertEqual(peak_active, 1000)
        self.assertEqual(ping_mock.await_count, 1000)


if __name__ == "__main__":
    unittest.main()