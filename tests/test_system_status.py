from __future__ import annotations

import pytest

from desktop_assistant.config import AppCatalog
from desktop_assistant.intent.models import ActionPlan, IntentKind, IntentResult, ToolAction
from desktop_assistant.models import RiskLevel, ToolArguments, ToolResult
from desktop_assistant.router import CommandRouter, DeterministicAction
from desktop_assistant.system_status import (
    BatteryInfo,
    DiskInfo,
    MemoryInfo,
    SystemStatusCollector,
    SystemStatusError,
    SystemStatusMetric,
    SystemStatusTool,
    WindowsSystemStatusCollector,
)
from conftest import FakeLauncher, FakeSystemStatusCollector, make_registry
from test_assistant_plan import FakeProvider, make_test_assistant


def test_cpu_status_shape_and_bounds() -> None:
    collector = FakeSystemStatusCollector(cpu_percent=42.5)
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "cpu"),)))
    assert not isinstance(prep, ToolResult if hasattr(prep, "success") else type(None))

    result = tool.execute(prep.execution_value)
    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert result.message == "CPU utilization: 42.5%."
    assert result.details == {"metric": "cpu", "cpu_percent": 42.5}
    assert 0.0 <= result.details["cpu_percent"] <= 100.0


def test_memory_status_shape_and_percentage_bounds() -> None:
    mem = MemoryInfo(
        used_bytes=4 * (1024 ** 3),
        total_bytes=16 * (1024 ** 3),
        percent=25.0,
    )
    collector = FakeSystemStatusCollector(memory=mem)
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "memory"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert "Memory: 4.0 GB used of 16.0 GB (25%)." in result.message
    assert result.details["used_bytes"] == 4 * (1024 ** 3)
    assert result.details["total_bytes"] == 16 * (1024 ** 3)
    assert result.details["percent"] == 25.0


def test_battery_status_when_present_charging() -> None:
    battery = BatteryInfo(
        has_battery=True,
        percent=92,
        is_charging=True,
        ac_connected=True,
    )
    collector = FakeSystemStatusCollector(battery=battery)
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "battery"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert result.message == "Battery: 92% (plugged in, charging)."
    assert result.details["has_battery"] is True
    assert result.details["percent"] == 92
    assert result.details["is_charging"] is True


def test_battery_status_when_present_on_battery() -> None:
    battery = BatteryInfo(
        has_battery=True,
        percent=65,
        is_charging=False,
        ac_connected=False,
    )
    collector = FakeSystemStatusCollector(battery=battery)
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "battery"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert result.message == "Battery: 65% (on battery)."
    assert result.details["percent"] == 65
    assert result.details["ac_connected"] is False


def test_battery_status_when_absent() -> None:
    battery = BatteryInfo(has_battery=False)
    collector = FakeSystemStatusCollector(battery=battery)
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "battery"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert result.message == "No battery detected."
    assert result.details == {"metric": "battery", "has_battery": False}


def test_disk_status_calculation() -> None:
    disk = DiskInfo(
        drive="C:",
        total_bytes=512 * (1024 ** 3),
        used_bytes=256 * (1024 ** 3),
        free_bytes=256 * (1024 ** 3),
        percent=50.0,
    )
    collector = FakeSystemStatusCollector(disk=disk)
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "disk"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert result.message == "System drive (C:): 256.0 GB free of 512.0 GB (50% used)."
    assert result.details["drive"] == "C:"
    assert result.details["free_bytes"] == 256 * (1024 ** 3)
    assert result.details["total_bytes"] == 512 * (1024 ** 3)
    assert result.details["percent"] == 50.0


def test_overview_aggregation_with_battery() -> None:
    collector = FakeSystemStatusCollector(
        cpu_percent=12.5,
        memory=MemoryInfo(used_bytes=8 * (1024 ** 3), total_bytes=16 * (1024 ** 3), percent=50.0),
        disk=DiskInfo(drive="C:", total_bytes=500 * (1024 ** 3), used_bytes=200 * (1024 ** 3), free_bytes=300 * (1024 ** 3), percent=40.0),
        battery=BatteryInfo(has_battery=True, percent=88, is_charging=False, ac_connected=False),
    )
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "overview"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert "System Overview:" in result.message
    assert "CPU: 12.5%" in result.message
    assert "Memory: 8.0 GB / 16.0 GB (50%)" in result.message
    assert "Disk (C:): 300.0 GB free of 500.0 GB (40% used)" in result.message
    assert "Battery: 88% (on battery)" in result.message
    assert result.details["cpu_percent"] == 12.5
    assert result.details["has_battery"] is True


def test_overview_aggregation_without_battery() -> None:
    collector = FakeSystemStatusCollector(
        cpu_percent=5.0,
        battery=BatteryInfo(has_battery=False),
    )
    tool = SystemStatusTool(collector)

    prep = tool.prepare(ToolArguments((("metric", "overview"),)))
    result = tool.execute(prep.execution_value)

    assert result.success
    assert "Battery: No battery detected" in result.message
    assert result.details["has_battery"] is False


def test_invalid_metric_rejected() -> None:
    tool = SystemStatusTool(FakeSystemStatusCollector())

    prep = tool.prepare(ToolArguments((("metric", "gpu"),)))
    assert not prep.success
    assert "Invalid system status metric 'gpu'" in prep.message

    exec_result = tool.execute("invalid")
    assert not exec_result.success
    assert "The prepared system status action is invalid." in exec_result.message

    bad_type = tool.execute(12345)
    assert not bad_type.success
    assert "The prepared system status action is invalid." in bad_type.message


def test_collector_api_failure_returns_safe_tool_result() -> None:
    class FailingCollector:
        def get_cpu_percent(self) -> float:
            raise SystemStatusError("Failed to read system times")

        def get_memory_info(self) -> MemoryInfo:
            raise SystemStatusError("Failed to read memory status")

        def get_battery_info(self) -> BatteryInfo:
            raise SystemStatusError("Power status unavailable")

        def get_disk_info(self) -> DiskInfo:
            raise SystemStatusError("Disk unavailable")

    tool = SystemStatusTool(FailingCollector())

    for metric in ("cpu", "memory", "battery", "disk", "overview"):
        result = tool.execute(metric)
        assert not result.success
        assert result.risk_level == RiskLevel.SAFE
        assert "Could not retrieve" in result.message or "Failed to retrieve" in result.message


def test_router_deterministic_system_status_commands() -> None:
    launcher = FakeLauncher()
    registry = make_registry(launcher)
    router = CommandRouter(registry, AppCatalog())

    cases = [
        ("cpu", "system_status", {"metric": "cpu"}),
        ("check cpu", "system_status", {"metric": "cpu"}),
        ("cpu usage", "system_status", {"metric": "cpu"}),
        ("ram", "system_status", {"metric": "memory"}),
        ("memory", "system_status", {"metric": "memory"}),
        ("check ram", "system_status", {"metric": "memory"}),
        ("check memory", "system_status", {"metric": "memory"}),
        ("ram usage", "system_status", {"metric": "memory"}),
        ("memory usage", "system_status", {"metric": "memory"}),
        ("battery", "system_status", {"metric": "battery"}),
        ("check battery", "system_status", {"metric": "battery"}),
        ("battery status", "system_status", {"metric": "battery"}),
        ("disk", "system_status", {"metric": "disk"}),
        ("check disk", "system_status", {"metric": "disk"}),
        ("disk space", "system_status", {"metric": "disk"}),
        ("check disk space", "system_status", {"metric": "disk"}),
        ("system status", "system_status", {"metric": "overview"}),
        ("system overview", "system_status", {"metric": "overview"}),
        ("check system", "system_status", {"metric": "overview"}),
        ("overview", "system_status", {"metric": "overview"}),
        ("system", "system_status", {"metric": "overview"}),
    ]

    for command, expected_tool, expected_args in cases:
        decision = router.route_detailed(command)
        assert decision.recognized, f"Failed to recognize command: {command}"
        assert decision.action == DeterministicAction(expected_tool, expected_args)


def test_multi_step_plan_with_cpu_and_memory() -> None:
    launcher = FakeLauncher()
    collector = FakeSystemStatusCollector(
        cpu_percent=20.0,
        memory=MemoryInfo(used_bytes=6 * (1024 ** 3), total_bytes=16 * (1024 ** 3), percent=37.5),
    )
    plan = ActionPlan(
        (
            ToolAction("system_status", {"metric": "cpu"}),
            ToolAction("system_status", {"metric": "memory"}),
        )
    )
    provider = FakeProvider(IntentResult.action_plan(plan.actions))
    assistant = make_test_assistant(
        launcher,
        provider,
        system_status_collector=collector,
    )

    response = assistant.handle("Πες μου CPU και RAM")

    assert response.kind.value == "completed"
    assert "CPU utilization: 20.0%." in response.message
    assert "Memory: 6.0 GB used of 16.0 GB (38%)." in response.message


def test_windows_collector_real_apis_smoke() -> None:
    """Smoke test real Windows collector on host machine; bounds and safety."""
    import os
    if os.name != "nt":
        pytest.skip("Windows only smoke test")

    collector = WindowsSystemStatusCollector(sample_interval=0.01)

    cpu = collector.get_cpu_percent()
    assert 0.0 <= cpu <= 100.0

    mem = collector.get_memory_info()
    assert mem.total_bytes > 0
    assert 0 <= mem.used_bytes <= mem.total_bytes
    assert 0.0 <= mem.percent <= 100.0

    disk = collector.get_disk_info()
    assert disk.total_bytes > 0
    assert disk.free_bytes >= 0
    assert 0.0 <= disk.percent <= 100.0

    battery = collector.get_battery_info()
    assert isinstance(battery.has_battery, bool)
    if battery.has_battery:
        assert battery.percent is not None
        assert 0 <= battery.percent <= 100
