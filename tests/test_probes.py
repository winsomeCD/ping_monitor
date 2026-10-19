import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from icmplib import NameLookupError, SocketPermissionError

from probes import (
    MAX_CONCURRENT_PROBES,
    AsyncICMPSession,
    DNSResolver,
    ProbeResult,
    ProbeTask,
    normalize_targets,
    probe_target,
)


class TargetValidationTests(unittest.TestCase):
    def test_supplied_domain_list_count_and_duplicates(self):
        domain_list = Path(__file__).resolve().parents[1] / "ping_domains.txt"
        raw_targets = domain_list.read_text(encoding="utf-8").strip()

        targets, duplicate_count = normalize_targets(raw_targets)

        self.assertEqual(len(raw_targets.split(",")), 500)
        self.assertEqual(len(targets), 500)
        self.assertEqual(duplicate_count, 0)

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


class DNSResolverTests(unittest.TestCase):
    def test_resolver_recognizes_ip_addresses(self):
        resolver = DNSResolver()
        self.assertTrue(resolver.is_ip("192.168.1.1"))
        self.assertTrue(resolver.is_ip("8.8.8.8"))
        self.assertTrue(resolver.is_ip("2001:4860:4860::8888"))
        self.assertFalse(resolver.is_ip("google.com"))

    def test_resolver_returns_ip_immediately_without_lookup(self):
        resolver = DNSResolver()
        result = asyncio.run(resolver.resolve("1.1.1.1"))
        self.assertEqual(result, "1.1.1.1")

    def test_resolver_caches_results(self):
        resolver = DNSResolver(ttl=60.0)
        resolver._cache["example.com"] = ("93.184.216.34", 9999999999.0)
        result = asyncio.run(resolver.resolve("example.com"))
        self.assertEqual(result, "93.184.216.34")

    def test_resolver_pre_resolve_all(self):
        resolver = DNSResolver(ttl=60.0)
        resolver._cache["example.com"] = ("93.184.216.34", 9999999999.0)
        results = asyncio.run(resolver.pre_resolve_all(["1.1.1.1", "example.com"]))
        self.assertEqual(results, {"example.com": "93.184.216.34"})


class AsyncICMPSessionTests(unittest.TestCase):
    def test_checksum_calculation(self):
        data = b"\x08\x00\x00\x00\x12\x34\x00\x01test_payload"
        chk = AsyncICMPSession._checksum(data)
        self.assertIsInstance(chk, int)
        self.assertTrue(0 <= chk <= 0xFFFF)

    def test_session_lifecycle(self):
        session = AsyncICMPSession(payload_size=32)
        self.assertEqual(session.payload_size, 32)
        self.assertFalse(session._is_active)
        session.close()
        self.assertFalse(session._is_active)


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
    def test_probe_target_with_destination_ip(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=True, avg_rtt=5.5)
        result = asyncio.run(
            probe_target("router.local", 56, 1.5, destination_ip="192.168.1.1")
        )
        self.assertEqual(result, ProbeResult("Connected", 5.5))
        ping_mock.assert_awaited_once_with(
            "192.168.1.1", count=1, timeout=1.5, privileged=False, payload_size=56
        )

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_target_maps_no_reply_to_timeout(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=False, avg_rtt=0)
        result = asyncio.run(probe_target("192.0.2.1", 56, 0.2))
        self.assertEqual(result, ProbeResult("No ICMP Reply"))

    @patch("probes.async_ping", new_callable=AsyncMock, side_effect=NameLookupError("bad host"))
    def test_probe_target_maps_dns_errors(self, _ping_mock):
        result = asyncio.run(probe_target("bad host", 56, 0.2))
        self.assertEqual(result.status, "DNS Error")
        self.assertFalse(result.packet_sent)

    @patch("probes.async_ping", new_callable=AsyncMock, side_effect=SocketPermissionError("permission denied"))
    def test_probe_target_maps_permission_errors(self, _ping_mock):
        result = asyncio.run(probe_target("router.local", 56, 0.2))
        self.assertEqual(result.status, "Permission Denied")

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_task_emits_a_result_and_stops(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=True, avg_rtt=1.25)
        task = ProbeTask(8, ["127.0.0.1"], 56, 1.0, 0.1)
        emitted = []
        task.signals.completed.connect(lambda *values: (emitted.append(values), task.request_stop()))

        task.run()

        self.assertEqual(emitted, [(8, "127.0.0.1", ProbeResult("Connected", 1.25))])
        self.assertEqual(ping_mock.await_count, 1)

    @patch("probes.async_ping", new_callable=AsyncMock, side_effect=RuntimeError("unexpected failure"))
    def test_probe_task_converts_unexpected_errors(self, _ping_mock):
        task = ProbeTask(2, ["127.0.0.1"], 56, 1.0, 0.1)
        emitted = []
        task.signals.completed.connect(lambda *values: (emitted.append(values), task.request_stop()))

        task.run()

        self.assertEqual(emitted[0][0:2], (2, "127.0.0.1"))
        self.assertEqual(emitted[0][2].status, "Probe Error")
        self.assertIn("unexpected failure", emitted[0][2].error)

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_task_bounds_concurrent_targets(self, ping_mock):
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
        targets = [f"10.0.0.{index % 254 + 1}" for index in range(1000)]
        task = ProbeTask(9, targets, 56, 1.0, 1.0, max_concurrent=250)
        completed = []

        def on_completed(*values):
            completed.append(values)
            if len(completed) == len(targets):
                task.request_stop()

        task.signals.completed.connect(on_completed)
        task.run()

        self.assertEqual(len(completed), 1000)
        self.assertEqual(task.max_concurrent, 250)
        self.assertLessEqual(peak_active, 250)
        self.assertEqual(ping_mock.await_count, 1000)

    @patch("probes.async_ping", new_callable=AsyncMock)
    def test_probe_task_emits_batches(self, ping_mock):
        ping_mock.return_value = SimpleNamespace(is_alive=True, avg_rtt=1.5)
        targets = ["10.0.0.1", "10.0.0.2"]
        task = ProbeTask(11, targets, 56, 1.0, 0.1)
        batches = []

        def on_batch(run_id, batch):
            batches.append((run_id, batch))
            task.request_stop()

        task.signals.batch_completed.connect(on_batch)
        task.run()

        self.assertTrue(len(batches) >= 1)
        self.assertEqual(batches[0][0], 11)
        flattened = [item[0] for _, b in batches for item in b]
        self.assertTrue(any(t in flattened for t in targets))

    @patch("probes.DNSResolver.resolve", new_callable=AsyncMock)
    def test_probe_task_retires_dns_error_host_after_retry(self, resolve_mock):
        resolve_mock.side_effect = NameLookupError("Mock lookup failure")
        target = "unresolvable-node.invalid"
        task = ProbeTask(22, [target], 56, 1.0, 0.05)
        emitted_results = []

        task.signals.completed.connect(
            lambda run_id, host, res: emitted_results.append((host, res.status))
        )
        task.run()

        self.assertEqual(len(emitted_results), 1)
        self.assertEqual(emitted_results[0], (target, "DNS Error"))
        # 1 pre-resolve call + 1 initial monitor attempt + 1 retry = 3 resolve calls
        self.assertEqual(resolve_mock.await_count, 3)


if __name__ == "__main__":
    unittest.main()

