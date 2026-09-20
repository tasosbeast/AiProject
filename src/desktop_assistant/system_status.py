from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Protocol

from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult


logger = logging.getLogger(__name__)


class SystemStatusError(RuntimeError):
    """Raised when querying a system metric fails."""


class SystemStatusMetric(str, Enum):
    CPU = "cpu"
    MEMORY = "memory"
    BATTERY = "battery"
    DISK = "disk"
    OVERVIEW = "overview"


@dataclass(frozen=True, slots=True)
class MemoryInfo:
    used_bytes: int
    total_bytes: int
    percent: float


@dataclass(frozen=True, slots=True)
class BatteryInfo:
    has_battery: bool
    percent: int | None = None
    is_charging: bool = False
    ac_connected: bool = False


@dataclass(frozen=True, slots=True)
class DiskInfo:
    drive: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float


class SystemStatusCollector(Protocol):
    def get_cpu_percent(self) -> float: ...

    def get_memory_info(self) -> MemoryInfo: ...

    def get_battery_info(self) -> BatteryInfo: ...

    def get_disk_info(self) -> DiskInfo: ...


def _format_bytes_gb(byte_count: int) -> str:
    return f"{byte_count / (1024 ** 3):.1f} GB"


class WindowsSystemStatusCollector:
    """Collects system status metrics on Windows using native APIs and stdlib."""

    def __init__(self, sample_interval: float = 0.05) -> None:
        self._sample_interval = sample_interval

    def get_cpu_percent(self) -> float:
        if os.name != "nt":
            raise SystemStatusError("CPU monitoring is only supported on Windows.")

        try:
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

            def ft_to_int(ft: FILETIME) -> int:
                return (ft.dwHighDateTime << 32) + ft.dwLowDateTime

            i1, k1, u1 = FILETIME(), FILETIME(), FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(i1), ctypes.byref(k1), ctypes.byref(u1)
            ):
                raise SystemStatusError("GetSystemTimes call failed.")

            time.sleep(self._sample_interval)

            i2, k2, u2 = FILETIME(), FILETIME(), FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(i2), ctypes.byref(k2), ctypes.byref(u2)
            ):
                raise SystemStatusError("GetSystemTimes call failed.")

            idle = ft_to_int(i2) - ft_to_int(i1)
            kernel = ft_to_int(k2) - ft_to_int(k1)
            user = ft_to_int(u2) - ft_to_int(u1)
            total_sys = kernel + user

            if total_sys <= 0:
                return 0.0

            busy = total_sys - idle
            if busy < 0:
                busy = 0
            pct = (busy / total_sys) * 100.0
            return max(0.0, min(100.0, round(pct, 1)))
        except SystemStatusError:
            raise
        except Exception as exc:
            raise SystemStatusError("Could not retrieve CPU utilization.") from exc

    def get_memory_info(self) -> MemoryInfo:
        if os.name != "nt":
            raise SystemStatusError("Memory monitoring is only supported on Windows.")

        try:
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                raise SystemStatusError("GlobalMemoryStatusEx call failed.")

            total = stat.ullTotalPhys
            avail = stat.ullAvailPhys
            used = max(0, total - avail)
            pct = round((used / total) * 100.0, 1) if total > 0 else 0.0
            return MemoryInfo(used_bytes=used, total_bytes=total, percent=pct)
        except SystemStatusError:
            raise
        except Exception as exc:
            raise SystemStatusError("Could not retrieve memory statistics.") from exc

    def get_battery_info(self) -> BatteryInfo:
        if os.name != "nt":
            raise SystemStatusError("Battery monitoring is only supported on Windows.")

        try:
            from ctypes import wintypes

            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", wintypes.DWORD),
                    ("BatteryFullLifeTime", wintypes.DWORD),
                ]

            pwr = SYSTEM_POWER_STATUS()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
                raise SystemStatusError("GetSystemPowerStatus call failed.")

            # BatteryFlag bit 7 (128) means no system battery; BatteryLifePercent == 255 (or -1 as signed byte) means unknown/no battery
            percent_raw = pwr.BatteryLifePercent
            if percent_raw < 0:
                percent_raw += 256

            battery_flag_raw = pwr.BatteryFlag
            if battery_flag_raw < 0:
                battery_flag_raw += 256

            has_no_battery = bool(battery_flag_raw & 128) or (percent_raw == 255)
            if has_no_battery:
                return BatteryInfo(has_battery=False)

            percent = max(0, min(100, percent_raw))
            ac_connected = (pwr.ACLineStatus == 1)
            is_charging = bool(battery_flag_raw & 8)

            return BatteryInfo(
                has_battery=True,
                percent=percent,
                is_charging=is_charging,
                ac_connected=ac_connected,
            )
        except SystemStatusError:
            raise
        except Exception as exc:
            raise SystemStatusError("Could not retrieve battery status.") from exc

    def get_disk_info(self) -> DiskInfo:
        try:
            system_drive = os.environ.get("SystemDrive", "C:")
            if not system_drive.endswith("\\"):
                system_drive += "\\"

            usage = shutil.disk_usage(system_drive)
            total = usage.total
            free = usage.free
            used = usage.used
            pct = round((used / total) * 100.0, 1) if total > 0 else 0.0
            return DiskInfo(
                drive=system_drive.rstrip("\\"),
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                percent=pct,
            )
        except Exception as exc:
            raise SystemStatusError("Could not retrieve disk usage.") from exc


class SystemStatusTool:
    name = "system_status"
    risk_level = RiskLevel.SAFE

    def __init__(self, collector: SystemStatusCollector) -> None:
        self._collector = collector

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        raw_metric = arguments.get("metric")
        try:
            metric = SystemStatusMetric(str(raw_metric))
        except ValueError:
            valid = ", ".join(m.value for m in SystemStatusMetric)
            return ToolResult(
                False,
                f"Invalid system status metric '{raw_metric}'. Supported metrics: {valid}.",
                self.risk_level,
            )
        return ToolPreparation(metric.value, ToolArguments((("metric", metric.value),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, str):
            return ToolResult(False, "The prepared system status action is invalid.", self.risk_level)
        try:
            metric = SystemStatusMetric(prepared_value)
        except ValueError:
            return ToolResult(False, "The prepared system status action is invalid.", self.risk_level)

        try:
            if metric is SystemStatusMetric.CPU:
                return self._execute_cpu()
            if metric is SystemStatusMetric.MEMORY:
                return self._execute_memory()
            if metric is SystemStatusMetric.BATTERY:
                return self._execute_battery()
            if metric is SystemStatusMetric.DISK:
                return self._execute_disk()
            if metric is SystemStatusMetric.OVERVIEW:
                return self._execute_overview()
            return ToolResult(False, "Unsupported system status metric.", self.risk_level)
        except SystemStatusError as exc:
            logger.warning("System status error for %s: %s", metric.value, exc)
            return ToolResult(False, f"Could not retrieve {metric.value} status: {exc}", self.risk_level)
        except Exception:
            logger.exception("Unexpected error while querying system status metric %s", metric.value)
            return ToolResult(False, f"Failed to retrieve {metric.value} status.", self.risk_level)

    def _execute_cpu(self) -> ToolResult:
        pct = self._collector.get_cpu_percent()
        message = f"CPU utilization: {pct:.1f}%."
        return ToolResult(True, message, self.risk_level, {"metric": "cpu", "cpu_percent": pct})

    def _execute_memory(self) -> ToolResult:
        mem = self._collector.get_memory_info()
        used_gb = _format_bytes_gb(mem.used_bytes)
        total_gb = _format_bytes_gb(mem.total_bytes)
        message = f"Memory: {used_gb} used of {total_gb} ({mem.percent:.0f}%)."
        return ToolResult(
            True,
            message,
            self.risk_level,
            {
                "metric": "memory",
                "used_bytes": mem.used_bytes,
                "total_bytes": mem.total_bytes,
                "percent": mem.percent,
            },
        )

    def _execute_battery(self) -> ToolResult:
        battery = self._collector.get_battery_info()
        if not battery.has_battery:
            return ToolResult(
                True,
                "No battery detected.",
                self.risk_level,
                {"metric": "battery", "has_battery": False},
            )

        if battery.is_charging:
            state = "plugged in, charging"
        elif battery.ac_connected:
            state = "plugged in"
        else:
            state = "on battery"

        message = f"Battery: {battery.percent}% ({state})."
        return ToolResult(
            True,
            message,
            self.risk_level,
            {
                "metric": "battery",
                "has_battery": True,
                "percent": battery.percent,
                "is_charging": battery.is_charging,
                "ac_connected": battery.ac_connected,
            },
        )

    def _execute_disk(self) -> ToolResult:
        disk = self._collector.get_disk_info()
        free_gb = _format_bytes_gb(disk.free_bytes)
        total_gb = _format_bytes_gb(disk.total_bytes)
        message = f"System drive ({disk.drive}): {free_gb} free of {total_gb} ({disk.percent:.0f}% used)."
        return ToolResult(
            True,
            message,
            self.risk_level,
            {
                "metric": "disk",
                "drive": disk.drive,
                "total_bytes": disk.total_bytes,
                "used_bytes": disk.used_bytes,
                "free_bytes": disk.free_bytes,
                "percent": disk.percent,
            },
        )

    def _execute_overview(self) -> ToolResult:
        cpu_pct = self._collector.get_cpu_percent()
        mem = self._collector.get_memory_info()
        disk = self._collector.get_disk_info()
        battery = self._collector.get_battery_info()

        lines = [
            "System Overview:",
            f"- CPU: {cpu_pct:.1f}%",
            f"- Memory: {_format_bytes_gb(mem.used_bytes)} / {_format_bytes_gb(mem.total_bytes)} ({mem.percent:.0f}%)",
            f"- Disk ({disk.drive}): {_format_bytes_gb(disk.free_bytes)} free of {_format_bytes_gb(disk.total_bytes)} ({disk.percent:.0f}% used)",
        ]

        if battery.has_battery:
            if battery.is_charging:
                b_state = "plugged in, charging"
            elif battery.ac_connected:
                b_state = "plugged in"
            else:
                b_state = "on battery"
            lines.append(f"- Battery: {battery.percent}% ({b_state})")
        else:
            lines.append("- Battery: No battery detected")

        message = "\n".join(lines)
        return ToolResult(
            True,
            message,
            self.risk_level,
            {
                "metric": "overview",
                "cpu_percent": cpu_pct,
                "memory_percent": mem.percent,
                "disk_percent": disk.percent,
                "has_battery": battery.has_battery,
            },
        )
