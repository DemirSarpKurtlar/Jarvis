"""Sistem izleme: CPU, RAM, disk, ağ, çalışma süresi ve NVIDIA GPU.

Yalnızca gerçek sensör verisi döner. Bir değer okunamıyorsa uydurulmaz;
`None` döner ve nedeni `gpu_unavailable_reason` gibi alanlarda açıklanır.

`sample()` engelleyici olabilir (NVML sürücü çağrıları), bu yüzden arayüz
thread'inde değil, arka plan executor'ünde çağrılmalıdır.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)

_GB = 1024**3
_MB = 1024**2


@dataclass(frozen=True)
class GpuInfo:
    name: str
    temperature_c: int | None
    utilization_pct: int | None
    memory_used_mb: int | None
    memory_total_mb: int | None
    fan_pct: int | None


@dataclass(frozen=True)
class SystemSnapshot:
    timestamp: float
    cpu_percent: float
    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float
    disk_path: str
    disk_free_gb: float
    disk_total_gb: float
    disk_percent: float
    net_down_bps: float | None   # ilk örnekte None (hız için iki ölçüm gerekir)
    net_up_bps: float | None
    uptime_s: float
    gpu: GpuInfo | None
    gpu_unavailable_reason: str | None


def system_disk_path() -> str:
    if sys.platform == "win32":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


class _NvidiaGpu:
    """NVML üzerinden ilk NVIDIA GPU'yu okur. Yoksa nedeni saklar."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self._nvml = None
        self._handle = None
        try:
            import pynvml  # nvidia-ml-py paketi

            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                self.reason = "NVIDIA ekran kartı bulunamadı"
                pynvml.nvmlShutdown()
                return
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(self._handle)
            self._name = name.decode() if isinstance(name, bytes) else str(name)
            log.info("GPU izleme etkin: %s", self._name)
        except ImportError:
            self.reason = "nvidia-ml-py kurulu değil"
        except Exception as exc:  # NVMLError: sürücü yok, kart yok vb.
            self.reason = "NVIDIA sürücüsüne ulaşılamadı"
            log.info("GPU izleme devre dışı: %s", exc)

    @property
    def available(self) -> bool:
        return self._handle is not None

    def _opt(self, fn, *args):  # noqa: ANN001, ANN202
        """Desteklenmeyen sensörler (örn. fan) için None döndür."""
        try:
            return fn(self._handle, *args)
        except Exception:
            return None

    def read(self) -> GpuInfo | None:
        if not self.available:
            return None
        n = self._nvml
        temp = self._opt(n.nvmlDeviceGetTemperature, n.NVML_TEMPERATURE_GPU)
        util = self._opt(n.nvmlDeviceGetUtilizationRates)
        mem = self._opt(n.nvmlDeviceGetMemoryInfo)
        fan = self._opt(n.nvmlDeviceGetFanSpeed)
        return GpuInfo(
            name=self._name,
            temperature_c=int(temp) if temp is not None else None,
            utilization_pct=int(util.gpu) if util is not None else None,
            memory_used_mb=int(mem.used // _MB) if mem is not None else None,
            memory_total_mb=int(mem.total // _MB) if mem is not None else None,
            fan_pct=int(fan) if fan is not None else None,
        )

    def close(self) -> None:
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass
            self._nvml = None
            self._handle = None


class SystemMonitor:
    def __init__(self, disk_path: str | None = None) -> None:
        self.disk_path = disk_path or system_disk_path()
        psutil.cpu_percent(interval=None)  # ilk çağrı referans noktası oluşturur
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.monotonic()
        self._first = True
        self._gpu = _NvidiaGpu()

    def sample(self) -> SystemSnapshot:
        now = time.monotonic()
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(self.disk_path)

        net = psutil.net_io_counters()
        dt = now - self._last_net_t
        if self._first or dt <= 0:
            down = up = None
        else:
            down = max(0.0, (net.bytes_recv - self._last_net.bytes_recv) * 8 / dt)
            up = max(0.0, (net.bytes_sent - self._last_net.bytes_sent) * 8 / dt)
        self._last_net, self._last_net_t, self._first = net, now, False

        return SystemSnapshot(
            timestamp=time.time(),
            cpu_percent=psutil.cpu_percent(interval=None),
            ram_used_gb=(vm.total - vm.available) / _GB,
            ram_total_gb=vm.total / _GB,
            ram_percent=vm.percent,
            disk_path=self.disk_path,
            disk_free_gb=du.free / _GB,
            disk_total_gb=du.total / _GB,
            disk_percent=du.percent,
            net_down_bps=down,
            net_up_bps=up,
            uptime_s=time.time() - psutil.boot_time(),
            gpu=self._gpu.read(),
            gpu_unavailable_reason=self._gpu.reason,
        )

    def close(self) -> None:
        self._gpu.close()
