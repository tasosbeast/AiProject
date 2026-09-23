from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from typing import Iterator, Protocol
import os
from time import monotonic, sleep


class EditableControlError(RuntimeError):
    """A target-scoped editable control could not be focused safely."""


class FocusedEditableControl(Protocol):
    def is_focused(self) -> bool: ...


class EditableControlResolver(Protocol):
    def focus(self, handle: int, process_id: int) -> AbstractContextManager[FocusedEditableControl]: ...


class AutomationBackend(Protocol):
    def open(self) -> AbstractContextManager[tuple[object, object]]: ...


class WindowsAutomationBackend:
    @contextmanager
    def open(self) -> Iterator[tuple[object, object]]:
        if os.name != "nt":
            raise EditableControlError("UI Automation is available only on Windows.")
        # Imported only for text actions. Keep COM apartment lifetime on the same
        # worker thread through the final focus check and SendInput call.
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        try:
            types = comtypes.client.GetModule("UIAutomationCore.dll")
            automation = comtypes.client.CreateObject(types.CUIAutomation, interface=types.IUIAutomation)
            yield automation, types
        finally:
            comtypes.CoUninitialize()


class _FocusedControl:
    def __init__(self, automation: object, types: object, root: object,
                 element: object, handle: int, process_id: int) -> None:
        self._automation = automation
        self._types = types
        self._root = root
        self._element = element
        self._handle = handle
        self._process_id = process_id

    def _within_root(self, element: object) -> bool:
        walker = self._automation.RawViewWalker
        for _ in range(64):
            if self._automation.CompareElements(element, self._root):
                return True
            # Do not walk into the desktop or another top-level window.
            if element.CurrentControlType == self._types.UIA_WindowControlTypeId:
                return False
            element = walker.GetParentElement(element)
            if element is None:
                return False
        return False

    def is_focused(self) -> bool:
        try:
            if (self._root.CurrentNativeWindowHandle != self._handle
                    or self._root.CurrentProcessId != self._process_id):
                return False
            if (self._element.CurrentProcessId != self._process_id
                    or not self._element.CurrentIsEnabled
                    or not self._element.CurrentIsKeyboardFocusable
                    or not self._within_root(self._element)):
                return False
            focused = self._automation.GetFocusedElement()
            if (focused is None or focused.CurrentProcessId != self._process_id
                    or not focused.CurrentHasKeyboardFocus
                    or not self._within_root(focused)):
                return False
            # Some document providers focus an inner element rather than the
            # document itself. That inner element must stay in this editor.
            if (self._automation.CompareElements(focused, self._element)
                    or self._is_inside_editor(focused)):
                return True
        except Exception:
            return False
        return False

    def _is_inside_editor(self, focused: object) -> bool:
        walker = self._automation.RawViewWalker
        for _ in range(64):
            if self._automation.CompareElements(focused, self._element):
                return True
            if focused.CurrentControlType == self._types.UIA_WindowControlTypeId:
                return False
            focused = walker.GetParentElement(focused)
            if focused is None:
                return False
        return False


class WindowsEditableControlResolver:
    """Find and focus one editable Document/Edit in an exact top-level HWND."""

    def __init__(self, backend: AutomationBackend | None = None,
                 focus_timeout_seconds: float = 0.3) -> None:
        self._backend = backend or WindowsAutomationBackend()
        self._focus_timeout_seconds = focus_timeout_seconds

    @contextmanager
    def focus(self, handle: int, process_id: int) -> Iterator[FocusedEditableControl]:
        with self._backend.open() as (automation, types):
            try:
                root = automation.ElementFromHandle(handle)
                if (root is None or root.CurrentNativeWindowHandle != handle
                        or root.CurrentProcessId != process_id):
                    raise EditableControlError("The prepared window identity changed. No input sent.")
                document = automation.CreatePropertyCondition(
                    types.UIA_ControlTypePropertyId, types.UIA_DocumentControlTypeId,
                )
                edit = automation.CreatePropertyCondition(
                    types.UIA_ControlTypePropertyId, types.UIA_EditControlTypeId,
                )
                condition = automation.CreateOrCondition(document, edit)
                found = root.FindAll(types.TreeScope_Descendants, condition)
                candidates: list[tuple[int, object]] = []
                checker = _FocusedControl(automation, types, root, root, handle, process_id)
                for index in range(found.Length):
                    element = found.GetElement(index)
                    if self._eligible(element, process_id, types) and checker._within_root(element):
                        rank = 2 if element.CurrentControlType == types.UIA_DocumentControlTypeId else 1
                        candidates.append((rank, element))
                if not candidates:
                    raise EditableControlError("No editable text control could be identified")
                best_rank = max(rank for rank, _ in candidates)
                best = [element for rank, element in candidates if rank == best_rank]
                if len(best) != 1:
                    raise EditableControlError("Multiple equally suitable editable text controls were found")
                selected = best[0]
                # Recheck its identity and ancestry immediately before focusing it.
                if not self._eligible(selected, process_id, types) or not checker._within_root(selected):
                    raise EditableControlError("The editable control changed before focus. No input sent.")
                selected.SetFocus()
                focused = _FocusedControl(automation, types, root, selected, handle, process_id)
                deadline = monotonic() + self._focus_timeout_seconds
                while not focused.is_focused():
                    if monotonic() >= deadline:
                        raise EditableControlError("The editable text control could not be focused. No input sent.")
                    sleep(0.02)
            except EditableControlError:
                raise
            except Exception:
                # Provider/COM errors never include user text in the displayed result.
                raise EditableControlError("The editable text control could not be verified. No input sent.") from None
            # Keep the COM apartment alive for the SendInput guard. Exceptions from
            # the caller pass through unchanged (including partial-send failures).
            yield focused

    @staticmethod
    def _pattern(element: object, pattern_id: int, interface: object) -> object | None:
        try:
            raw = element.GetCurrentPattern(pattern_id)
            return raw.QueryInterface(interface) if raw is not None else None
        except Exception:
            return None

    @classmethod
    def _eligible(cls, element: object, process_id: int, types: object) -> bool:
        try:
            if (element.CurrentProcessId != process_id or not element.CurrentIsEnabled
                    or not element.CurrentIsKeyboardFocusable or element.CurrentIsPassword):
                return False
            if element.CurrentControlType not in (
                    types.UIA_DocumentControlTypeId, types.UIA_EditControlTypeId):
                return False
            value = cls._pattern(element, types.UIA_ValuePatternId, types.IUIAutomationValuePattern)
            if value is not None and value.CurrentIsReadOnly is False:
                return True
            text = cls._pattern(element, types.UIA_TextPatternId, types.IUIAutomationTextPattern)
            if text is None:
                return False
            readonly = text.DocumentRange.GetAttributeValue(types.UIA_IsReadOnlyAttributeId)
            if readonly is False:
                return True
            # Rich editors can expose TextEditPattern even when the provider
            # cannot return an IsReadOnly text attribute for an empty document.
            return (readonly is not True and cls._pattern(
                element, types.UIA_TextEditPatternId, types.IUIAutomationTextEditPattern,
            ) is not None)
        except Exception:
            return False
