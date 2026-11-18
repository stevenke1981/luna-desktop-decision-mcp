"""Windows text delivery to the standard Unicode multiline Edit control."""
from __future__ import annotations

import ctypes
import ntpath
from collections.abc import Callable
from ctypes import wintypes

from desktop import FocusLost, TextDeliveryUnknown


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                ('rcCaret', wintypes.RECT)]


_user32 = ctypes.WinDLL('user32', use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo))
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
_user32.GetAncestor.restype = wintypes.HWND
_user32.WindowFromPoint.argtypes = (wintypes.POINT,)
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.IsWindowUnicode.argtypes = (wintypes.HWND,)
_user32.IsWindowEnabled.argtypes = (wintypes.HWND,)
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.SendMessageTimeoutW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
)
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E
EM_GETSEL, EM_REPLACESEL = 0x00B0, 0x00C2
ES_MULTILINE, ES_PASSWORD, ES_READONLY = 0x0004, 0x0020, 0x0800
_BROWSER_EXECUTABLES = {
    'arc.exe', 'brave.exe', 'chrome.exe', 'chromium.exe', 'firefox.exe', 'floorp.exe',
    'iexplore.exe', 'librewolf.exe', 'msedge.exe', 'msedgewebview2.exe', 'opera.exe',
    'tor.exe', 'vivaldi.exe', 'waterfox.exe', 'zen.exe',
}


def foreground_focus() -> tuple[int, int]:
    foreground = _user32.GetForegroundWindow() or 0
    info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
    if (_user32.GetGUIThreadInfo(0, ctypes.byref(info)) and info.hwndActive == foreground
            and info.hwndFocus and _user32.GetAncestor(info.hwndFocus, 2) == foreground):  # GA_ROOT
        return foreground, info.hwndFocus
    return foreground, 0


def _message(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> int | None:
    result = ctypes.c_size_t()
    # SMTO_BLOCK | SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT.
    ok = _user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, 0x0023, 200, ctypes.byref(result))
    return result.value if ok else None


def _edit_text(hwnd: int) -> str | None:
    length = _message(hwnd, WM_GETTEXTLENGTH)
    if length is None or length >= 0xFFFF:
        return None
    value = ctypes.create_unicode_buffer(length + 1)
    copied = _message(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(value))
    return value.value if copied == length else None


def insert_multiline(text: str, focus: tuple[int, int], point: tuple[int, int],
                     guard: Callable[[], None]) -> bool:
    """Insert at the caret or replace the current selection, then read it back.

    False means there was no write and the caller may use its normal paste path.
    A write with an unknown result raises, so callers cannot fall through to paste.
    """
    hwnd = _user32.WindowFromPoint(wintypes.POINT(*point))
    if (not hwnd or '\0' in text or _user32.GetAncestor(hwnd, 2) != focus[0]
            or not _user32.IsWindowUnicode(hwnd) or not _user32.IsWindowEnabled(hwnd)):
        return False
    name = ctypes.create_unicode_buffer(32)
    _user32.GetClassNameW(hwnd, name, len(name))
    if name.value.casefold() != 'edit':
        return False
    style = _user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
    if not style & ES_MULTILINE or style & (ES_READONLY | ES_PASSWORD):
        return False
    if hwnd != focus[1]:
        raise FocusLost('The native edit at the target point has no keyboard focus.')
    pid = wintypes.DWORD()
    if not _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return False
    process = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    path = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(path))
    try:
        if (not _kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size))
                or ntpath.basename(path.value).casefold() in _BROWSER_EXECUTABLES):
            return False
    finally:
        _kernel32.CloseHandle(process)

    guard()
    prior = _edit_text(hwnd)
    selection = _message(hwnd, EM_GETSEL)
    if prior is None or selection is None or selection >= 0xFFFFFFFF:
        return False
    start, end = selection & 0xFFFF, selection >> 16
    old = prior.encode('utf-16-le', errors='surrogatepass')
    if not 0 <= start <= end <= len(old) // 2:
        return False
    # Win32 Edit expects a CRLF hard line break. Selection positions count UTF-16 units.
    native_text = text.replace('\n', '\r\n')
    expected = old[:2 * start] + native_text.encode('utf-16-le') + old[2 * end:]
    if len(expected) >= 2 * 0xFFFF:
        return False
    value = ctypes.create_unicode_buffer(native_text)
    guard()
    if _message(hwnd, EM_REPLACESEL, 1, ctypes.addressof(value)) is None:
        raise TextDeliveryUnknown('The native edit did not confirm text delivery.')
    actual = _edit_text(hwnd)
    if actual is None or actual.encode('utf-16-le', errors='surrogatepass') != expected:
        raise TextDeliveryUnknown('The native edit readback did not match the expected text.')
    return True
