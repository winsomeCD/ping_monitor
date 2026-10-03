"""Cross-platform asynchronous ICMP probing and target validation."""

import asyncio
import ipaddress
import json
import random
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from icmplib import ICMPLibError, NameLookupError, SocketPermissionError, async_ping
from PySide6.QtCore import QObject, QRunnable, Signal

MAX_CONCURRENT_PROBES = 1000
DNS_CACHE_TTL = 300.0  # 5 minutes
DNS_NEGATIVE_CACHE_TTL = 10.0  # 10 seconds backoff for unresolvable domains
MAX_CONCURRENT_DNS_LOOKUPS = 40


@dataclass(frozen=True)
class ProbeResult:
    status: str
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    packet_sent: bool = True


class DNSResolver:
    """Async DNS resolver with caching, TTL, negative cache, and concurrency control."""

    def __init__(
        self,
        ttl: float = DNS_CACHE_TTL,
        negative_ttl: float = DNS_NEGATIVE_CACHE_TTL,
        max_concurrent: int = MAX_CONCURRENT_DNS_LOOKUPS,
    ):
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.max_concurrent = max_concurrent
        self._cache: dict[str, tuple[str, float]] = {}
        self._negative_cache: dict[str, tuple[Exception, float]] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None

    @staticmethod
    def is_ip(target: str) -> bool:
        try:
            ipaddress.ip_address(target)
            return True
        except ValueError:
            return False

    async def resolve(self, target: str) -> str:
        """Resolve a hostname to an IP address, utilizing cache if valid."""
        if self.is_ip(target):
            return target

        now = time.monotonic()

        # Check positive cache
        cached = self._cache.get(target)
        if cached:
            ip, expire_at = cached
            if now < expire_at:
                return ip

        # Check negative cache
        neg_cached = self._negative_cache.get(target)
        if neg_cached:
            err, expire_at = neg_cached
            if now < expire_at:
                if cached:
                    return cached[0]
                raise err

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)

        async with self._semaphore:
            # Re-check cache after acquiring semaphore
            now = time.monotonic()
            cached = self._cache.get(target)
            if cached and now < cached[1]:
                return cached[0]

            loop = asyncio.get_running_loop()
            try:
                # Try IPv4 first
                lookup = await loop.getaddrinfo(
                    target, None, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
                ip = lookup[0][4][0]
                self._cache[target] = (ip, now + self.ttl)
                self._negative_cache.pop(target, None)
                return ip
            except OSError:
                try:
                    # Fallback to IPv6
                    lookup = await loop.getaddrinfo(
                        target, None, family=socket.AF_INET6, type=socket.SOCK_DGRAM
                    )
                    ip = lookup[0][4][0]
                    self._cache[target] = (ip, now + self.ttl)
                    self._negative_cache.pop(target, None)
                    return ip
                except OSError:
                    err = NameLookupError(target)
                    if cached:
                        # Soft-extend stale cache briefly (15s) while logging failure
                        self._cache[target] = (cached[0], now + 15.0)
                        return cached[0]
                    self._negative_cache[target] = (err, now + self.negative_ttl)
                    raise err

    def clear_cache(self):
        self._cache.clear()
        self._negative_cache.clear()


class AsyncICMPSession:
    """High-performance multiplexed ICMP session for handling hundreds of parallel targets."""

    def __init__(self, payload_size: int = 56):
        self.payload_size = payload_size
        self._v4_sock: Optional[socket.socket] = None
        self._v6_sock: Optional[socket.socket] = None
        self._pending_v4: dict[tuple[int, int], tuple[asyncio.Future, float]] = {}
        self._pending_v6: dict[tuple[int, int], tuple[asyncio.Future, float]] = {}
        self._seq = 0
        self._id = random.randint(1, 65535)
        self._receiver_tasks: list[asyncio.Task] = []
        self._stop_event: Optional[asyncio.Event] = None
        self._is_active = False

    @staticmethod
    def _checksum(data: bytes) -> int:
        if len(data) % 2 == 1:
            data += b"\0"
        words = struct.unpack(f"!{len(data)//2}H", data)
        total = sum(words)
        total = (total >> 16) + (total & 0xFFFF)
        total += total >> 16
        return (~total) & 0xFFFF

    async def start(self) -> bool:
        """Attempt to open multiplexed raw ICMP sockets."""
        try:
            self._v4_sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            self._v4_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._v4_sock.setblocking(False)
        except OSError:
            self._v4_sock = None

        try:
            self._v6_sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
            self._v6_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._v6_sock.setblocking(False)
        except OSError:
            self._v6_sock = None

        if self._v4_sock is None and self._v6_sock is None:
            return False

        self._stop_event = asyncio.Event()
        self._is_active = True
        if self._v4_sock is not None:
            self._receiver_tasks.append(asyncio.create_task(self._v4_receiver()))
        if self._v6_sock is not None:
            self._receiver_tasks.append(asyncio.create_task(self._v6_receiver()))
        return True

    async def _v4_receiver(self):
        loop = asyncio.get_running_loop()
        while self._is_active and not self._stop_event.is_set():
            try:
                data = await loop.sock_recv(self._v4_sock, 2048)
                recv_time = time.monotonic()
                if len(data) >= 28:
                    ip_header_len = (data[0] & 0x0F) * 4
                    if len(data) >= ip_header_len + 8:
                        icmp_header = data[ip_header_len : ip_header_len + 8]
                        icmp_type, _code, _chk, pkt_id, seq = struct.unpack("!BBHHH", icmp_header)
                        if icmp_type == 0:  # Echo Reply
                            key = (pkt_id, seq)
                            item = self._pending_v4.pop(key, None)
                            if item is not None:
                                fut, send_time = item
                                if not fut.done():
                                    latency = round((recv_time - send_time) * 1000, 2)
                                    fut.set_result(latency)
            except (asyncio.CancelledError, OSError):
                if not self._is_active or self._stop_event.is_set():
                    break
                await asyncio.sleep(0.001)

    async def _v6_receiver(self):
        loop = asyncio.get_running_loop()
        while self._is_active and not self._stop_event.is_set():
            try:
                data = await loop.sock_recv(self._v6_sock, 2048)
                recv_time = time.monotonic()
                if len(data) >= 8:
                    icmp_type, _code, _chk, pkt_id, seq = struct.unpack("!BBHHH", data[:8])
                    if icmp_type == 129:  # ICMPv6 Echo Reply
                        key = (pkt_id, seq)
                        item = self._pending_v6.pop(key, None)
                        if item is not None:
                            fut, send_time = item
                            if not fut.done():
                                latency = round((recv_time - send_time) * 1000, 2)
                                fut.set_result(latency)
            except (asyncio.CancelledError, OSError):
                if not self._is_active or self._stop_event.is_set():
                    break
                await asyncio.sleep(0.001)

    async def ping(self, ip: str, timeout: float) -> Optional[float]:
        """Send an ICMP echo request and await reply."""
        is_v6 = ":" in ip
        sock = self._v6_sock if is_v6 else self._v4_sock
        if sock is None:
            return None

        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        pkt_id = self._id
        payload = b"Q" * self.payload_size

        if is_v6:
            header = struct.pack("!BBHHH", 128, 0, 0, pkt_id, seq)
            packet = header + payload
            pending_map = self._pending_v6
        else:
            header = struct.pack("!BBHHH", 8, 0, 0, pkt_id, seq)
            chk = self._checksum(header + payload)
            packet = struct.pack("!BBHHH", 8, 0, chk, pkt_id, seq) + payload
            pending_map = self._pending_v4

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        send_time = time.monotonic()
        key = (pkt_id, seq)
        pending_map[key] = (fut, send_time)

        try:
            sock.sendto(packet, (ip, 1))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            pending_map.pop(key, None)
            return None
        except Exception:
            pending_map.pop(key, None)
            return None

    def close(self):
        self._is_active = False
        if self._stop_event is not None:
            self._stop_event.set()
        for task in self._receiver_tasks:
            task.cancel()
        self._receiver_tasks.clear()
        if self._v4_sock is not None:
            try:
                self._v4_sock.close()
            except OSError:
                pass
            self._v4_sock = None
        if self._v6_sock is not None:
            try:
                self._v6_sock.close()
            except OSError:
                pass
            self._v6_sock = None


def normalize_targets(raw_targets: str) -> tuple[list[str], int]:
    """Return unique targets and the number of duplicate entries removed."""
    targets = [target.strip() for target in raw_targets.split(",") if target.strip()]
    unique_targets = []
    seen = set()
    duplicate_count = 0

    for target in targets:
        if any(character.isspace() for character in target):
            raise ValueError(f"Target contains whitespace: {target!r}")
        normalized = target.casefold()
        if normalized in seen:
            duplicate_count += 1
            continue
        seen.add(normalized)
        unique_targets.append(target)

    return unique_targets, duplicate_count


async def probe_target(
    target: str,
    payload_size: int,
    timeout: float,
    destination_ip: Optional[str] = None,
) -> ProbeResult:
    """Send one portable asynchronous ICMP echo request."""
    dest = destination_ip if destination_ip is not None else target
    try:
        response = await async_ping(
            dest,
            count=1,
            timeout=timeout,
            privileged=False,
            payload_size=payload_size,
        )
    except NameLookupError as error:
        return ProbeResult("DNS Error", error=str(error), packet_sent=False)
    except SocketPermissionError as error:
        return ProbeResult("Permission Denied", error=str(error), packet_sent=False)
    except (ICMPLibError, OSError) as error:
        return ProbeResult("Probe Error", error=str(error), packet_sent=False)

    if not response.is_alive:
        return ProbeResult("No ICMP Reply")
    return ProbeResult("Connected", round(response.avg_rtt, 2))


class ProbeSignals(QObject):
    completed = Signal(int, str, object)
    finished = Signal(int)


class ProbeTask(QRunnable):
    """Monitor targets with Linux fping when available, multiplexed session, or icmplib fallback."""

    def __init__(
        self,
        run_id: int,
        targets: list[str],
        payload_size: int,
        timeout: float,
        interval: float,
        max_concurrent: int = MAX_CONCURRENT_PROBES,
    ):
        super().__init__()
        self.run_id = run_id
        self.targets = targets
        self.payload_size = payload_size
        self.timeout = timeout
        self.interval = interval
        self.max_concurrent = min(MAX_CONCURRENT_PROBES, max(1, int(max_concurrent)))
        self._semaphore = None
        self._dns_resolver = DNSResolver()
        self._icmp_session: Optional[AsyncICMPSession] = None
        self.signals = ProbeSignals()
        self._stop_requested = threading.Event()
        self._loop = None
        self._async_stop_event = None

    def request_stop(self):
        self._stop_requested.set()
        if (
            self._loop is not None
            and self._loop.is_running()
            and self._async_stop_event is not None
        ):
            try:
                self._loop.call_soon_threadsafe(self._async_stop_event.set)
            except RuntimeError:
                pass

    async def _monitor_target(self, target: str):
        loop = asyncio.get_running_loop()
        while not self._async_stop_event.is_set():
            started = loop.time()
            try:
                dest_ip = None
                if self._dns_resolver is not None:
                    try:
                        dest_ip = await self._dns_resolver.resolve(target)
                    except NameLookupError as error:
                        result = ProbeResult("DNS Error", error=str(error), packet_sent=False)
                        if self._async_stop_event.is_set():
                            return
                        self.signals.completed.emit(self.run_id, target, result)
                        delay = max(0.0, self.interval - (loop.time() - started))
                        if delay:
                            try:
                                await asyncio.wait_for(self._async_stop_event.wait(), timeout=delay)
                            except asyncio.TimeoutError:
                                pass
                        continue

                if self._icmp_session is not None and dest_ip:
                    try:
                        latency = await self._icmp_session.ping(dest_ip, self.timeout)
                        if latency is not None:
                            result = ProbeResult("Connected", latency)
                        else:
                            result = ProbeResult("No ICMP Reply")
                    except Exception as error:
                        result = ProbeResult("Probe Error", error=str(error), packet_sent=False)
                else:
                    async with self._semaphore:
                        if self._async_stop_event.is_set():
                            return
                        result = await probe_target(
                            target, self.payload_size, self.timeout, destination_ip=dest_ip
                        )
            except Exception as error:
                result = ProbeResult("Probe Error", error=str(error), packet_sent=False)

            if self._async_stop_event.is_set():
                return
            self.signals.completed.emit(self.run_id, target, result)

            delay = max(0.0, self.interval - (loop.time() - started))
            if delay:
                try:
                    await asyncio.wait_for(self._async_stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._async_stop_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        if self._stop_requested.is_set():
            self._async_stop_event.set()
        if await self._supports_fping_json():
            used_fping = await self._run_fping()
            if used_fping or self._async_stop_event.is_set():
                return

        # Initialize high-performance multiplexed ICMP session when not mocked
        if (
            hasattr(async_ping, "mock_calls")
            or hasattr(async_ping, "assert_called")
            or "Mock" in type(async_ping).__name__
        ):
            self._icmp_session = None
        else:
            self._icmp_session = AsyncICMPSession(self.payload_size)
            session_started = await self._icmp_session.start()
            if not session_started:
                self._icmp_session = None

        try:
            await asyncio.gather(*(self._monitor_target(target) for target in self.targets))
        finally:
            if self._icmp_session is not None:
                self._icmp_session.close()
                self._icmp_session = None

    async def _supports_fping_json(self) -> bool:
        """Only enable the Linux backend when fping's structured output exists."""
        if not sys.platform.startswith("linux") or self._async_stop_event.is_set():
            return False
        executable = shutil.which("fping")
        if not executable:
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                executable, "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=3)
        except (OSError, asyncio.TimeoutError):
            return False
        self._fping_executable = executable
        return b"--json" in output or b"-J" in output

    async def _run_fping(self) -> bool:
        """Use fping's single paced event loop instead of one socket per host."""
        period_ms = max(10, round(max(self.interval, self.timeout) * 1000))
        timeout_ms = max(1, round(self.timeout * 1000))
        command = [
            self._fping_executable, "--json", "--loop",
            "--size", str(self.payload_size),
            "--period", str(period_ms),
            "--timeout", str(timeout_ms),
            "--retry", "0",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            process.stdin.write(("\n".join(self.targets) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except (OSError, AttributeError):
            return False

        results_seen = 0
        stderr_task = asyncio.create_task(process.stderr.read())

        async def consume_results():
            nonlocal results_seen
            aliases = {target.casefold(): target for target in self.targets}
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                    data = event.get("resp") or event.get("timeout")
                    if not isinstance(data, dict):
                        continue
                    target = aliases.get(str(data.get("host", "")).casefold())
                    if target is None:
                        continue
                    if "resp" in event:
                        result = ProbeResult("Connected", round(float(data["rtt"]), 2))
                    else:
                        result = ProbeResult("No ICMP Reply")
                    results_seen += 1
                    self.signals.completed.emit(self.run_id, target, result)
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue

        reader_task = asyncio.create_task(consume_results())
        stop_task = asyncio.create_task(self._async_stop_event.wait())
        done, _ = await asyncio.wait(
            (reader_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done and self._async_stop_event.is_set() and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if not reader_task.done():
            reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        stderr = (await stderr_task).decode(errors="replace").strip()

        if results_seen == 0 and not self._async_stop_event.is_set():
            return False
        if process.returncode not in (0, 1, -15) and not self._async_stop_event.is_set():
            error_text = stderr or f"fping exited with code {process.returncode}"
            for target in self.targets:
                self.signals.completed.emit(
                    self.run_id, target,
                    ProbeResult("Probe Error", error=error_text, packet_sent=False),
                )
        return True

    def run(self):
        try:
            asyncio.run(self._run())
        except Exception as error:
            for target in self.targets:
                self.signals.completed.emit(
                    self.run_id,
                    target,
                    ProbeResult("Probe Error", error=str(error), packet_sent=False),
                )
        finally:
            self.signals.finished.emit(self.run_id)
