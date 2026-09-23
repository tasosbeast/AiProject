"""Read-only, target-scoped Windows UI Automation control metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from desktop_assistant.config import AppCatalog
from desktop_assistant.editable_controls import AutomationBackend, WindowsAutomationBackend
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult
from desktop_assistant.process_control import WindowController
from desktop_assistant.windows import bounded_window_label, match_window_for_focus


MAX_CONTROLS = 40
MAX_NAME = 120
MAX_AUTOMATION_ID = 80


def _bounded(value: object, limit: int) -> str:
    # Accessible names can include line breaks or terminal control characters.
    clean = " ".join("".join(c if c.isprintable() else " " for c in str(value or "")).split())
    return clean[:limit]


@dataclass(frozen=True, slots=True)
class PreparedUITarget:
    handle: int
    process_id: int
    title: str
    executable_name: str


@dataclass(frozen=True, slots=True)
class UIElementInfo:
    control_type: str
    name: str
    automation_id: str
    enabled: bool
    focusable: bool
    focused: bool
    offscreen: bool
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UIInspection:
    controls: tuple[UIElementInfo, ...]
    truncated: bool


class UIInspectionError(RuntimeError):
    """UI Automation could not safely inspect the prepared window."""


class UIInspector(Protocol):
    def inspect(self, handle: int, process_id: int) -> UIInspection: ...


# Explicit allowlist; UIA's numeric control IDs are obtained from its generated typelib.
_CONTROL_TYPES = (
    ("Button", "button"), ("Edit", "edit"), ("Document", "document"),
    ("CheckBox", "check_box"), ("RadioButton", "radio_button"),
    ("ComboBox", "combo_box"), ("List", "list"), ("ListItem", "list_item"),
    ("Tab", "tab"), ("TabItem", "tab_item"), ("Menu", "menu"),
    ("MenuItem", "menu_item"), ("Hyperlink", "hyperlink"),
    ("Tree", "tree"), ("TreeItem", "tree_item"), ("DataItem", "data_item"),
    ("Slider", "slider"), ("Spinner", "spinner"),
)
_PATTERNS = (
    ("Invoke", "invoke"), ("SelectionItem", "select"),
    ("Toggle", "toggle"), ("ExpandCollapse", "expand_collapse"),
    ("Scroll", "scroll"), ("RangeValue", "range"),
)


class WindowsUIInspector:
    """Enumerate only allowlisted descendants of ElementFromHandle(handle)."""

    def __init__(self, backend: AutomationBackend | None = None) -> None:
        self._backend = backend or WindowsAutomationBackend()

    def inspect(self, handle: int, process_id: int) -> UIInspection:
        try:
            with self._backend.open() as (automation, types):
                root = automation.ElementFromHandle(handle)
                if (root is None or root.CurrentNativeWindowHandle != handle
                        or root.CurrentProcessId != process_id):
                    raise UIInspectionError("The prepared window identity changed.")
                type_names = {
                    getattr(types, f"UIA_{raw}ControlTypeId"): friendly
                    for raw, friendly in _CONTROL_TYPES
                }
                conditions = [
                    automation.CreatePropertyCondition(types.UIA_ControlTypePropertyId, control_id)
                    for control_id in type_names
                ]
                condition = conditions[0]
                for next_condition in conditions[1:]:
                    condition = automation.CreateOrCondition(condition, next_condition)
                # FindAll is rooted in the prepared HWND, never the desktop. The
                # ancestry check below also rejects a provider's stray result.
                found = root.FindAll(types.TreeScope_Descendants, condition)
                controls: list[UIElementInfo] = []
                truncated = False
                for index in range(found.Length):
                    element = found.GetElement(index)
                    if (element.CurrentProcessId != process_id
                            or not self._within_root(automation, types, root, element)):
                        continue
                    friendly = type_names.get(element.CurrentControlType)
                    if friendly is None:
                        continue
                    if len(controls) == MAX_CONTROLS:
                        truncated = True
                        break
                    controls.append(self._read_info(element, types, friendly))
                if (root.CurrentNativeWindowHandle != handle
                        or root.CurrentProcessId != process_id):
                    raise UIInspectionError("The prepared window identity changed.")
                return UIInspection(tuple(controls), truncated)
        except UIInspectionError:
            raise
        except Exception:
            # COM/provider exceptions may include private control text.
            raise UIInspectionError("The window controls could not be inspected safely.") from None

    @staticmethod
    def _within_root(automation: object, types: object, root: object, element: object) -> bool:
        walker = automation.RawViewWalker
        for _ in range(64):
            if automation.CompareElements(element, root):
                return True
            if element.CurrentControlType == types.UIA_WindowControlTypeId:
                return False
            element = walker.GetParentElement(element)
            if element is None:
                return False
        return False

    @staticmethod
    def _available(element: object, types: object, pattern: str) -> bool:
        return bool(element.GetCurrentPropertyValue(
            getattr(types, f"UIA_Is{pattern}PatternAvailablePropertyId"),
        ))

    @classmethod
    def _read_info(cls, element: object, types: object, friendly: str) -> UIElementInfo:
        password = bool(element.CurrentIsPassword)
        name = "password field" if password else _bounded(element.CurrentName, MAX_NAME)
        automation_id = "" if password else _bounded(element.CurrentAutomationId, MAX_AUTOMATION_ID)
        capabilities = [
            label for pattern, label in _PATTERNS
            if cls._available(element, types, pattern)
        ]
        if not password and friendly in {"edit", "document"}:
            value = cls._available(element, types, "Value")
            text_edit = cls._available(element, types, "TextEdit")
            if text_edit or (value and element.GetCurrentPropertyValue(
                    types.UIA_ValueIsReadOnlyPropertyId) is False):
                capabilities.append("edit")
        return UIElementInfo(
            friendly, name, automation_id,
            bool(element.CurrentIsEnabled),
            bool(element.CurrentIsKeyboardFocusable),
            bool(element.CurrentHasKeyboardFocus),
            bool(element.CurrentIsOffscreen),
            tuple(capabilities),
        )


class UIInspectTool:
    name = "ui_inspect"
    risk_level = RiskLevel.SAFE

    def __init__(self, controller: WindowController, catalog: AppCatalog,
                 inspector: UIInspector) -> None:
        self._controller = controller
        self._catalog = catalog
        self._inspector = inspector

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(False, "Window query must not be empty.", self.risk_level)
        query = query.strip()
        try:
            match = match_window_for_focus(query, self._controller.visible_windows(), self._catalog)
        except Exception:
            return ToolResult(False, "Failed to inspect visible windows.", self.risk_level)
        if isinstance(match, str):
            return ToolResult(False, match, self.risk_level)
        if not match.handle or not match.process_id or not match.title or not match.executable_name:
            return ToolResult(False, "The selected window has an incomplete identity.", self.risk_level)
        target = PreparedUITarget(match.handle, match.process_id, match.title, match.executable_name)
        return ToolPreparation(target, ToolArguments((("query", query),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedUITarget):
            return ToolResult(False, "The prepared UI inspection is invalid.", self.risk_level)
        target = prepared_value
        title = bounded_window_label(target.title)
        try:
            current = self._controller.get_window_info(target.handle)
            valid = self._controller.is_window_valid(target.handle, target.process_id)
            if (not valid or current is None or current.handle != target.handle
                    or current.process_id != target.process_id or current.title != target.title
                    or current.executable_name.casefold() != target.executable_name.casefold()):
                return ToolResult(False, f"Window '{title}' changed or is no longer available.", self.risk_level)
            inspection = self._inspector.inspect(target.handle, target.process_id)
            current = self._controller.get_window_info(target.handle)
            if (not self._controller.is_window_valid(target.handle, target.process_id)
                    or current is None or current.handle != target.handle
                    or current.process_id != target.process_id or current.title != target.title
                    or current.executable_name.casefold() != target.executable_name.casefold()):
                return ToolResult(False, f"Window '{title}' changed or is no longer available.", self.risk_level)
        except UIInspectionError:
            return ToolResult(False, "The window controls could not be inspected safely.", self.risk_level)
        except Exception:
            return ToolResult(False, "The window controls could not be inspected safely.", self.risk_level)
        safe_types = {friendly for _, friendly in _CONTROL_TYPES}
        safe_capabilities = {label for _, label in _PATTERNS} | {"edit"}
        controls = tuple(
            UIElementInfo(
                control.control_type,
                _bounded(control.name, MAX_NAME),
                _bounded(control.automation_id, MAX_AUTOMATION_ID),
                bool(control.enabled), bool(control.focusable), bool(control.focused),
                bool(control.offscreen),
                tuple(capability for capability in control.capabilities
                      if capability in safe_capabilities),
            )
            for control in inspection.controls[:MAX_CONTROLS]
            if control.control_type in safe_types
        )
        truncated = inspection.truncated or len(inspection.controls) > MAX_CONTROLS
        details = {
            "title": title,
            "controls": [asdict(control) for control in controls],
            "truncated": truncated,
        }
        if not controls:
            return ToolResult(True, f"No supported interactive controls were found in {title}.",
                              self.risk_level, details)
        lines = [f"Controls in {title}:"]
        for index, control in enumerate(controls, 1):
            flags = []
            if control.enabled:
                flags.append("enabled")
            if control.focused:
                flags.append("focused")
            flags.extend(control.capabilities)
            label = f" — {control.name}" if control.name else ""
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"{index}. {control.control_type}{label}{suffix}")
        if truncated:
            lines.append("Results truncated to the first 40 supported controls.")
        return ToolResult(True, "\n".join(lines), self.risk_level, details)
