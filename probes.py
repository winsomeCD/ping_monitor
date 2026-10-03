"""Cross-platform asynchronous ICMP probing and target validation."""

import asyncio
import threading
from dataclasses import dataclass
from typing import Optional

from icmplib import ICMPLibError, NameLookupError, SocketPermissionError, async_ping
from PySide6.QtCore import QObject, QRunnable, Signal

@dataclass(frozen=True)
class ProbeResult:
    status: str
    latency_ms: Optional[float] = None
    error: Optional[str] = None


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


async def probe_target(target: str, payload_size: int, timeout: float) -> ProbeResult:
    """Send one portable asynchronous ICMP echo request."""
    try:
        response = await async_ping(
            target,
            count=1,
            timeout=timeout,
            privileged=False,
            payload_size=payload_size,
        )
    except NameLookupError as error:
        return ProbeResult("DNS Error", error=str(error))
    except SocketPermissionError as error:
        return ProbeResult("Permission Denied", error=str(error))
    except (ICMPLibError, OSError) as error:
        return ProbeResult("Probe Error", error=str(error))

    if not response.is_alive:
        return ProbeResult("Timed Out")
    return ProbeResult("Connected", round(response.avg_rtt, 2))


class ProbeSignals(QObject):
    completed = Signal(int, str, object)
    finished = Signal(int)


class ProbeTask(QRunnable):
    """Monitor all targets concurrently in one asyncio event loop."""

    def __init__(
        self,
        run_id: int,
        targets: list[str],
        payload_size: int,
        timeout: float,
        interval: float,
    ):
        super().__init__()
        self.run_id = run_id
        self.targets = targets
        self.payload_size = payload_size
        self.timeout = timeout
        self.interval = interval
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
                result = await probe_target(target, self.payload_size, self.timeout)
            except Exception as error:
                result = ProbeResult("Probe Error", error=str(error))

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
        if self._stop_requested.is_set():
            self._async_stop_event.set()
        await asyncio.gather(*(self._monitor_target(target) for target in self.targets))

    def run(self):
        try:
            asyncio.run(self._run())
        except Exception as error:
            for target in self.targets:
                self.signals.completed.emit(
                    self.run_id,
                    target,
                    ProbeResult("Probe Error", error=str(error)),
                )
        finally:
            self.signals.finished.emit(self.run_id)