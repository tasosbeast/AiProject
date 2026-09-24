"""Confirmed Windows UI Automation actions scoped to an exact existing window."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Protocol, Sequence

from desktop_assistant.config import AppCatalog
from desktop_assistant.editable_controls import AutomationBackend, WindowsAutomationBackend
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult
from desktop_assistant.process_control import WindowController
from desktop_assistant.ui_perception import (
    MAX_AUTOMATION_ID,
    MAX_NAME,
    MAX_SCANNED_ELEMENTS,
    _CONTROL_TYPES,
    _bounded,
)
from desktop_assistant.windows import bounded_window_label, match_window_for_focus

logger = logging.getLogger(__name__)


class UIAction(str, Enum):
    INVOKE = "invoke"
    SELECT = "select"
    EXPAND = "expand"
    COLLAPSE = "collapse"
    TOGGLE_ON = "toggle_on"
    TOGGLE_OFF = "toggle_off"


_ACTION_PATTERN_NAMES = {
    UIAction.INVOKE: "Invoke",
    UIAction.SELECT: "SelectionItem",
    UIAction.EXPAND: "ExpandCollapse",
    UIAction.COLLAPSE: "ExpandCollapse",
    UIAction.TOGGLE_ON: "Toggle",
    UIAction.TOGGLE_OFF: "Toggle",
}


@dataclass(frozen=True, slots=True)
class PreparedControlIdentity:
    control_type: str
    name: str
    automation_id: str
    runtime_id: tuple[int, ...]
    current_toggle_state: str | None = None

    @property
    def toggle_state(self) -> str | None:
        return self.current_toggle_state


@dataclass(frozen=True, slots=True)
class PreparedUIAction:
    handle: int
    process_id: int
    title: str
    executable_name: str
    action: UIAction
    control_type: str
    name: str
    automation_id: str
    runtime_id: tuple[int, ...]
    desired_toggle_state: str | None = None

    @property
    def desired_state(self) -> str | None:
        return self.desired_toggle_state


class UIActionController(Protocol):
    def resolve_control(
        self,
        handle: int,
        process_id: int,
        control_name: str,
        action: UIAction,
    ) -> PreparedControlIdentity | str: ...

    def execute_action(
        self,
        target: PreparedUIAction,
    ) -> ToolResult | bool: ...


def _get_runtime_id(element: object, types: object) -> tuple[int, ...] | None:
    raw = None
    if hasattr(element, "GetRuntimeId"):
        try:
            raw = element.GetRuntimeId()
        except Exception:
            raw = None
    if raw is None and hasattr(element, "GetCurrentPropertyValue"):
        try:
            prop_id = getattr(types, "UIA_RuntimeIdPropertyId", None)
            if prop_id is not None:
                raw = element.GetCurrentPropertyValue(prop_id)
        except Exception:
            raw = None
    if raw is None:
        return None
    try:
        converted = tuple(int(x) for x in raw)
        return converted if converted else None
    except Exception:
        return None


def _read_pattern_toggle_state(
    pattern: object,
    on_val: object,
    off_val: object,
    indet_val: object,
) -> str | None:
    try:
        raw_state = getattr(pattern, "CurrentToggleState", None)
        if callable(raw_state):
            raw_state = raw_state()
        if raw_state == on_val:
            return "on"
        if raw_state == off_val:
            return "off"
        if raw_state == indet_val:
            return "indeterminate"
        return "unknown"
    except Exception:
        return None


def _execute_pattern_action(element: object, types: object, action: UIAction) -> None:
    if action == UIAction.INVOKE:
        pattern_id = getattr(types, "UIA_InvokePatternId", None)
        interface_name = "IUIAutomationInvokePattern"
        method_name = "Invoke"
    elif action == UIAction.SELECT:
        pattern_id = getattr(types, "UIA_SelectionItemPatternId", None)
        interface_name = "IUIAutomationSelectionItemPattern"
        method_name = "Select"
    elif action == UIAction.EXPAND:
        pattern_id = getattr(types, "UIA_ExpandCollapsePatternId", None)
        interface_name = "IUIAutomationExpandCollapsePattern"
        method_name = "Expand"
    elif action == UIAction.COLLAPSE:
        pattern_id = getattr(types, "UIA_ExpandCollapsePatternId", None)
        interface_name = "IUIAutomationExpandCollapsePattern"
        method_name = "Collapse"
    else:
        raise ValueError(f"Unsupported UI action: {action}")

    if pattern_id is None:
        raise RuntimeError(f"UI Automation pattern constant for {action.value} is unavailable.")

    raw_pattern = element.GetCurrentPattern(pattern_id)
    if raw_pattern is None:
        raise RuntimeError("The requested pattern is not available on the control.")
    pattern = raw_pattern
    if hasattr(pattern, "QueryInterface"):
        try:
            interface_cls = getattr(types, interface_name, None)
            if interface_cls is not None:
                pattern = pattern.QueryInterface(interface_cls)
        except Exception:
            pass
    method = getattr(pattern, method_name, None)
    if method is None or not callable(method):
        raise RuntimeError(f"The control pattern does not support {method_name}.")
    method()


def _execute_toggle_action(
    element: object,
    types: object,
    target: PreparedUIAction,
) -> ToolResult:
    pattern_id = getattr(types, "UIA_TogglePatternId", None)
    if pattern_id is None:
        raise RuntimeError("UI Automation TogglePatternId constant is unavailable.")

    on_val = getattr(types, "ToggleState_On", None)
    off_val = getattr(types, "ToggleState_Off", None)
    indet_val = getattr(types, "ToggleState_Indeterminate", None)
    if on_val is None or off_val is None or indet_val is None:
        raise RuntimeError("UI Automation ToggleState constants are unavailable.")

    raw_pattern = element.GetCurrentPattern(pattern_id)
    if raw_pattern is None:
        raise RuntimeError("The requested pattern is not available on the control.")
    pattern = raw_pattern
    if hasattr(pattern, "QueryInterface"):
        try:
            interface_cls = getattr(types, "IUIAutomationTogglePattern", None)
            if interface_cls is not None:
                pattern = pattern.QueryInterface(interface_cls)
        except Exception:
            pass

    current_state = _read_pattern_toggle_state(pattern, on_val, off_val, indet_val)
    if current_state not in ("on", "off"):
        return ToolResult(False, f"Failed to perform {target.action.value} on '{target.name}'.", RiskLevel.SENSITIVE)

    desired = target.desired_toggle_state or ("on" if target.action == UIAction.TOGGLE_ON else "off")
    title = bounded_window_label(target.title)

    if current_state == desired:
        return ToolResult(
            True,
            f"Performed {target.action.value} on '{target.name}' in {title}.",
            RiskLevel.SENSITIVE,
            {"action": target.action.value, "control": target.name, "window": target.title},
        )

    toggle_method = getattr(pattern, "Toggle", None)
    if toggle_method is None or not callable(toggle_method):
        raise RuntimeError("The control pattern does not support Toggle.")
    toggle_method()

    resulting_state = _read_pattern_toggle_state(pattern, on_val, off_val, indet_val)
    if resulting_state != desired:
        return ToolResult(False, f"Failed to perform {target.action.value} on '{target.name}'.", RiskLevel.SENSITIVE)

    return ToolResult(
        True,
        f"Performed {target.action.value} on '{target.name}' in {title}.",
        RiskLevel.SENSITIVE,
        {"action": target.action.value, "control": target.name, "window": target.title},
    )


class WindowsUIActionController:
    """Resolve and execute UI Automation actions using WindowsAutomationBackend."""

    def __init__(self, backend: AutomationBackend | None = None) -> None:
        self._backend = backend or WindowsAutomationBackend()

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
        try:
            prop_id = getattr(types, f"UIA_Is{pattern}PatternAvailablePropertyId", None)
            if prop_id is None:
                return False
            return bool(element.GetCurrentPropertyValue(prop_id))
        except Exception:
            return False

    @staticmethod
    def _get_element_toggle_state(element: object, types: object) -> str | None:
        pattern_id = getattr(types, "UIA_TogglePatternId", None)
        if pattern_id is None:
            return None
        on_val = getattr(types, "ToggleState_On", None)
        off_val = getattr(types, "ToggleState_Off", None)
        indet_val = getattr(types, "ToggleState_Indeterminate", None)
        if on_val is None or off_val is None or indet_val is None:
            return None

        raw_pattern = None
        try:
            raw_pattern = element.GetCurrentPattern(pattern_id)
        except Exception:
            return None
        if raw_pattern is None:
            return None

        pattern = raw_pattern
        if hasattr(pattern, "QueryInterface"):
            try:
                interface_cls = getattr(types, "IUIAutomationTogglePattern", None)
                if interface_cls is not None:
                    pattern = pattern.QueryInterface(interface_cls)
            except Exception:
                pass

        return _read_pattern_toggle_state(pattern, on_val, off_val, indet_val)

    def resolve_control(
        self,
        handle: int,
        process_id: int,
        control_name: str,
        action: UIAction,
    ) -> PreparedControlIdentity | str:
        q_norm = control_name.strip().casefold()
        if not q_norm:
            return "Control name must not be empty."

        pattern_name = _ACTION_PATTERN_NAMES.get(action)
        if pattern_name is None:
            return f"Unsupported UI action '{action}'."

        try:
            with self._backend.open() as (automation, types):
                root = automation.ElementFromHandle(handle)
                if (
                    root is None
                    or root.CurrentNativeWindowHandle != handle
                    or root.CurrentProcessId != process_id
                ):
                    return "The target window could not be verified."

                if action in (UIAction.TOGGLE_ON, UIAction.TOGGLE_OFF):
                    pattern_id = getattr(types, "UIA_TogglePatternId", None)
                    on_val = getattr(types, "ToggleState_On", None)
                    off_val = getattr(types, "ToggleState_Off", None)
                    indet_val = getattr(types, "ToggleState_Indeterminate", None)
                    if pattern_id is None or on_val is None or off_val is None or indet_val is None:
                        return "The target control could not be inspected safely."

                type_names = {}
                for raw, friendly in _CONTROL_TYPES:
                    try:
                        type_names[getattr(types, f"UIA_{raw}ControlTypeId")] = friendly
                    except Exception:
                        continue

                condition = automation.CreateTrueCondition()
                found = root.FindAll(types.TreeScope_Descendants, condition)
                count = found.Length
                candidates: list[PreparedControlIdentity] = []

                for index in range(min(count, MAX_SCANNED_ELEMENTS)):
                    try:
                        element = found.GetElement(index)
                        if element.CurrentProcessId != process_id:
                            continue
                        if not self._within_root(automation, types, root, element):
                            continue
                        if not bool(element.CurrentIsEnabled):
                            continue
                        if bool(getattr(element, "CurrentIsPassword", False)):
                            continue
                        if not self._available(element, types, pattern_name):
                            continue
                        friendly = type_names.get(element.CurrentControlType)
                        if friendly is None:
                            continue
                        runtime_id = _get_runtime_id(element, types)
                        if runtime_id is None:
                            continue
                        name = _bounded(element.CurrentName, MAX_NAME)
                        if not name.strip():
                            continue
                        automation_id = _bounded(element.CurrentAutomationId, MAX_AUTOMATION_ID)
                        toggle_state = None
                        if action in (UIAction.TOGGLE_ON, UIAction.TOGGLE_OFF):
                            toggle_state = self._get_element_toggle_state(element, types)

                        candidates.append(
                            PreparedControlIdentity(
                                control_type=friendly,
                                name=name,
                                automation_id=automation_id,
                                runtime_id=runtime_id,
                                current_toggle_state=toggle_state,
                            )
                        )
                    except Exception:
                        continue

                if (
                    root.CurrentNativeWindowHandle != handle
                    or root.CurrentProcessId != process_id
                ):
                    return "The target window could not be verified."

                matched = self._match_candidate(candidates, control_name)
                if isinstance(matched, str):
                    return matched

                if action in (UIAction.TOGGLE_ON, UIAction.TOGGLE_OFF):
                    if matched.current_toggle_state == "indeterminate":
                        return f"The target control '{matched.name}' is in an indeterminate state."
                    if matched.current_toggle_state not in ("on", "off"):
                        return f"The target control '{matched.name}' does not expose a supported toggle state."

                return matched
        except Exception as exc:
            logger.warning("UI control resolution failed: %s", type(exc).__name__)
            return "The target control could not be inspected safely."

    @staticmethod
    def _match_candidate(
        candidates: Sequence[PreparedControlIdentity],
        query: str,
    ) -> PreparedControlIdentity | str:
        q_norm = query.strip().casefold()
        exact = [c for c in candidates if c.name.strip().casefold() == q_norm]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            candidates_str = ", ".join(f"{c.control_type} — '{c.name}'" for c in exact[:3])
            return f"Multiple matching controls found for '{query}': {candidates_str}."

        sub = [c for c in candidates if q_norm in c.name.strip().casefold()]
        if len(sub) == 1:
            return sub[0]
        if len(sub) > 1:
            candidates_str = ", ".join(f"{c.control_type} — '{c.name}'" for c in sub[:3])
            return f"Multiple matching controls found for '{query}': {candidates_str}."

        return "No matching actionable control found."

    def execute_action(self, target: PreparedUIAction) -> ToolResult:
        pattern_name = _ACTION_PATTERN_NAMES.get(target.action)
        if pattern_name is None:
            return ToolResult(False, f"Unsupported UI action '{target.action.value}'.", RiskLevel.SENSITIVE)

        try:
            with self._backend.open() as (automation, types):
                root = automation.ElementFromHandle(target.handle)
                if (
                    root is None
                    or root.CurrentNativeWindowHandle != target.handle
                    or root.CurrentProcessId != target.process_id
                ):
                    return ToolResult(False, "The target window could not be verified.", RiskLevel.SENSITIVE)

                type_names = {}
                for raw, friendly in _CONTROL_TYPES:
                    try:
                        type_names[getattr(types, f"UIA_{raw}ControlTypeId")] = friendly
                    except Exception:
                        continue

                condition = automation.CreateTrueCondition()
                found = root.FindAll(types.TreeScope_Descendants, condition)
                count = found.Length
                matched_elements = []

                for index in range(min(count, MAX_SCANNED_ELEMENTS)):
                    try:
                        element = found.GetElement(index)
                        rid = _get_runtime_id(element, types)
                        if rid == target.runtime_id:
                            matched_elements.append(element)
                    except Exception:
                        continue

                if len(matched_elements) != 1:
                    return ToolResult(False, "The target control could not be located uniquely.", RiskLevel.SENSITIVE)

                element = matched_elements[0]

                if element.CurrentProcessId != target.process_id:
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if not self._within_root(automation, types, root, element):
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if type_names.get(element.CurrentControlType) != target.control_type:
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if bool(getattr(element, "CurrentIsPassword", False)):
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if _bounded(element.CurrentName, MAX_NAME) != target.name:
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if _bounded(element.CurrentAutomationId, MAX_AUTOMATION_ID) != target.automation_id:
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if not bool(element.CurrentIsEnabled):
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)
                if not self._available(element, types, pattern_name):
                    return ToolResult(False, "The target control has changed or is no longer available.", RiskLevel.SENSITIVE)

                if target.action in (UIAction.TOGGLE_ON, UIAction.TOGGLE_OFF):
                    return _execute_toggle_action(element, types, target)

                _execute_pattern_action(element, types, target.action)

                title = bounded_window_label(target.title)
                return ToolResult(
                    True,
                    f"Performed {target.action.value} on '{target.name}' in {title}.",
                    RiskLevel.SENSITIVE,
                    {"action": target.action.value, "control": target.name, "window": target.title},
                )
        except Exception as exc:
            logger.warning("UI action execution failed: %s", type(exc).__name__)
            return ToolResult(False, f"Failed to perform {target.action.value} on '{target.name}'.", RiskLevel.SENSITIVE)


class UIActionTool:
    name = "ui_action"
    risk_level = RiskLevel.SENSITIVE
    allowed_actions = tuple(action.value for action in UIAction)

    def __init__(
        self,
        windows: WindowController,
        catalog: AppCatalog,
        controller: UIActionController,
    ) -> None:
        self._windows = windows
        self._catalog = catalog
        self._controller = controller

    def _failure(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        if not {"query", "control", "action"}.issubset(arguments) or set(arguments) - {"query", "control", "action"}:
            return self._failure("Invalid UI action arguments.")

        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failure("Window query must not be empty.")
        query = query.strip()

        control = arguments.get("control")
        if not isinstance(control, str) or not control.strip():
            return self._failure("Control name must not be empty.")
        control = control.strip()

        action_raw = arguments.get("action")
        try:
            action = UIAction(action_raw)
        except (ValueError, TypeError):
            supported = ", ".join(self.allowed_actions)
            return self._failure(f"Unsupported UI action '{action_raw}'. Supported actions: {supported}.")

        try:
            match = match_window_for_focus(query, self._windows.visible_windows(), self._catalog)
        except Exception:
            return self._failure("Failed to inspect visible windows.")

        if isinstance(match, str):
            return self._failure(match)

        if not match.handle or not match.process_id or not match.title or not match.executable_name:
            return self._failure("The selected window has an incomplete identity.")

        identity = self._controller.resolve_control(
            match.handle, match.process_id, control, action
        )
        if isinstance(identity, str):
            return self._failure(identity)
        if not isinstance(identity, PreparedControlIdentity):
            return self._failure("The target control could not be prepared safely.")

        desired_toggle_state = None
        if action == UIAction.TOGGLE_ON:
            desired_toggle_state = "on"
        elif action == UIAction.TOGGLE_OFF:
            desired_toggle_state = "off"

        if action in (UIAction.TOGGLE_ON, UIAction.TOGGLE_OFF):
            if identity.current_toggle_state == "indeterminate":
                return self._failure(f"The target control '{identity.name}' is in an indeterminate state.")
            if identity.current_toggle_state not in ("on", "off"):
                return self._failure(f"The target control '{identity.name}' does not expose a supported toggle state.")

        prepared = PreparedUIAction(
            handle=match.handle,
            process_id=match.process_id,
            title=match.title,
            executable_name=match.executable_name,
            action=action,
            control_type=identity.control_type,
            name=identity.name,
            automation_id=identity.automation_id,
            runtime_id=identity.runtime_id,
            desired_toggle_state=desired_toggle_state,
        )

        normalized = ToolArguments((
            ("query", f"{bounded_window_label(match.title)} — {bounded_window_label(match.executable_name, 80)}"),
            ("control", f"{identity.control_type} — {identity.name}"),
            ("action", action.value),
        ))
        return ToolPreparation(prepared, normalized)

    @staticmethod
    def confirmation_summary(arguments: ToolArguments) -> str:
        return (
            f"Target window:\n{arguments['query']}\n\n"
            f"Control:\n{arguments['control']}\n\n"
            f"Action:\n{arguments['action']}"
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedUIAction):
            return self._failure("The prepared UI action is invalid.")
        target = prepared_value
        title = bounded_window_label(target.title)

        try:
            current = self._windows.get_window_info(target.handle)
            valid = self._windows.is_window_valid(target.handle, target.process_id)
            if (
                not valid
                or current is None
                or current.handle != target.handle
                or current.process_id != target.process_id
                or current.title != target.title
                or current.executable_name.casefold() != target.executable_name.casefold()
            ):
                return self._failure(f"Window '{title}' changed or is no longer available.")

            outcome = self._controller.execute_action(target)
            if isinstance(outcome, ToolResult):
                return outcome
            if not outcome:
                return self._failure(f"Failed to perform {target.action.value} on '{target.name}'.")
            return ToolResult(
                True,
                f"Performed {target.action.value} on '{target.name}' in {title}.",
                self.risk_level,
                {"action": target.action.value, "control": target.name, "window": target.title},
            )
        except Exception:
            return self._failure(f"Failed to perform {target.action.value} on '{target.name}'.")
