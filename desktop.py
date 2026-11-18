"""Screen capture, target annotation and (optional) action execution.

Coordinates are physical pixels of the primary monitor ("screen" space).
Screenshots may be downscaled; capture() reports the scale so callers that
work in image space can convert (screen = image * scale).
"""
from __future__ import annotations

import base64
import io
import os
import struct
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

if sys.platform == 'win32':  # must run before pyautogui/mss read metrics
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from PIL import Image, ImageDraw

MAX_WIDTH = int(os.environ.get('LUNA_SCREENSHOT_MAX_WIDTH', '1600'))


@dataclass
class Shot:
    image: Image.Image   # possibly downscaled and/or cropped
    scale: float         # screen pixels per image pixel
    width: int           # screen width
    height: int          # screen height
    ox: float = 0.0      # screen position of the image's top-left corner
    oy: float = 0.0

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return round(x * self.scale + self.ox), round(y * self.scale + self.oy)

    def to_image(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.ox) / self.scale, (y - self.oy) / self.scale

    def crop(self, left: float, top: float, right: float, bottom: float) -> 'Shot':
        """Sub-shot of a screen rectangle (e.g. one window); same scale."""
        l, t = self.to_image(max(left, 0), max(top, 0))
        r, b = self.to_image(min(right, self.width), min(bottom, self.height))
        l, t = max(0, int(l)), max(0, int(t))
        r, b = min(self.image.width, int(r)), min(self.image.height, int(b))
        if r - l < 8 or b - t < 8:
            return self
        img = self.image.crop((l, t, r, b))
        return Shot(img, self.scale, self.width, self.height,
                    self.ox + l * self.scale, self.oy + t * self.scale)


def capture(max_width: int = MAX_WIDTH) -> Shot:
    import mss
    with (mss.MSS() if hasattr(mss, 'MSS') else mss.mss()) as sct:
        raw = sct.grab(_primary(sct))
    img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
    scale = 1.0
    if max_width and img.width > max_width:
        scale = img.width / max_width
        img = img.resize((max_width, round(img.height / scale)), Image.LANCZOS)
    return Shot(img, scale, raw.size[0], raw.size[1])


def _primary(sct) -> dict:
    """The primary monitor sits at (0, 0); monitors[1] is not guaranteed to be it."""
    return next((m for m in sct.monitors[1:] if m['left'] == 0 and m['top'] == 0),
                sct.monitors[1])


def annotate(shot: Shot, action: dict) -> Image.Image:
    """Draw a crosshair at the action target (screen coords) so the judge sees it."""
    img = shot.image.copy()
    draw = ImageDraw.Draw(img)
    r = max(10, img.width // 80)
    if action.get('type') == 'drag' and action.get('path'):
        pts = [shot.to_image(*_pt(p)) for p in action['path']]
        draw.line(pts, fill=(255, 0, 0), width=4)
        for x, y in (pts[0], pts[-1]):
            draw.ellipse((x - r / 2, y - r / 2, x + r / 2, y + r / 2), outline=(255, 0, 0), width=3)
    elif action.get('x') is not None and action.get('y') is not None:
        # ring + ticks that stop short of the target, so the target stays readable
        x, y = shot.to_image(action['x'], action['y'])
        R = r * 1.6
        draw.ellipse((x - R, y - R, x + R, y + R), outline=(255, 0, 0), width=3)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            draw.line((x + dx * R, y + dy * R, x + dx * 2.4 * r, y + dy * 2.4 * r),
                      fill=(255, 0, 0), width=3)
    return img


IMAGE_FORMAT = os.environ.get('LUNA_IMAGE_FORMAT', 'JPEG').upper()


def to_data_url(img: Image.Image, fmt: str | None = None) -> str:
    fmt = (fmt or IMAGE_FORMAT).upper()
    return f'data:image/{fmt.lower()};base64,' + base64.b64encode(to_bytes(img, fmt)).decode()


def to_bytes(img: Image.Image, fmt: str = 'PNG') -> bytes:
    buf = io.BytesIO()
    if fmt.upper() in ('JPEG', 'JPG'):
        img.convert('RGB').save(buf, format='JPEG', quality=85)
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


def wait_stable(timeout: float = 2.0, interval: float = 0.12, tolerance: float = 1.5) -> float:
    """Return once two consecutive small grey frames are nearly equal (UI settled),
    or at timeout. The tolerance ignores small animations (spinners, waveforms)."""
    import mss
    from PIL import ImageChops, ImageStat
    start = time.time()
    prev = None
    time.sleep(0.3)               # let navigation / re-render start before comparing frames
    with (mss.MSS() if hasattr(mss, 'MSS') else mss.mss()) as sct:
        mon = _primary(sct)
        while time.time() - start < timeout:
            raw = sct.grab(mon)
            small = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX').convert('L').resize((96, 54))
            if prev is not None and ImageStat.Stat(ImageChops.difference(small, prev)).mean[0] < tolerance:
                break
            prev = small
            time.sleep(interval)
    return time.time() - start


def _pt(p) -> tuple[float, float]:
    return (p['x'], p['y']) if isinstance(p, dict) else (p[0], p[1])


KEY_MAP = {
    'CTRL': 'ctrl', 'CONTROL': 'ctrl', 'ALT': 'alt', 'SHIFT': 'shift', 'WIN': 'win',
    'META': 'win', 'SUPER': 'win', 'CMD': 'win', 'ENTER': 'enter', 'RETURN': 'enter',
    'ESC': 'esc', 'ESCAPE': 'esc', 'TAB': 'tab', 'SPACE': 'space', 'BACKSPACE': 'backspace',
    'DELETE': 'delete', 'DEL': 'delete', 'HOME': 'home', 'END': 'end', 'PAGEUP': 'pageup',
    'PAGEDOWN': 'pagedown', 'UP': 'up', 'DOWN': 'down', 'LEFT': 'left', 'RIGHT': 'right',
    'ARROWUP': 'up', 'ARROWDOWN': 'down', 'ARROWLEFT': 'left', 'ARROWRIGHT': 'right',
    'INSERT': 'insert', 'CAPSLOCK': 'capslock',
}


def map_key(k: str) -> str:
    return KEY_MAP.get(k.upper(), k.lower())


def validate(action: dict, shot: Shot | None = None) -> None:
    kind = action.get('type')
    allowed = {'click', 'double_click', 'right_click', 'move', 'drag', 'scroll',
               'type', 'keypress', 'wait'}
    if kind not in allowed:
        raise ValueError(f'Unsupported action type {kind!r}; expected one of {sorted(allowed)}')
    pts = []
    if kind in {'click', 'double_click', 'right_click', 'move', 'scroll'}:
        if action.get('x') is None or action.get('y') is None:
            raise ValueError(f'{kind} needs x and y')
        pts.append((action['x'], action['y']))
    if kind == 'drag':
        if not action.get('path') or len(action['path']) < 2:
            raise ValueError('drag needs a path with at least two points')
        pts += [_pt(p) for p in action['path']]
    if kind == 'type' and not isinstance(action.get('text'), str):
        raise ValueError('type needs text')
    if kind == 'keypress' and not action.get('keys'):
        raise ValueError('keypress needs keys')
    if shot:
        for x, y in pts:
            if not (0 <= x < shot.width and 0 <= y < shot.height):
                raise ValueError(f'point ({x}, {y}) is outside the primary screen '
                                 f'{shot.width}x{shot.height}')


class FocusLost(RuntimeError):
    """The original input target no longer has focus."""


class TextDeliveryUnknown(RuntimeError):
    """Some or all of the text may already have reached the target."""


@dataclass(frozen=True)
class TextDelivery:
    status: Literal['verified', 'dispatched']
    focus: tuple[int, int] | None


def execute(action: dict, *, expected_hwnd: int = 0,
            expected_focus: tuple[int, int] | None = None,
            native_edit: bool = False) -> TextDelivery | None:
    import pyautogui
    pyautogui.FAILSAFE = True   # slam the mouse into a corner to abort
    pyautogui.PAUSE = 0.05
    kind = action['type']
    button = action.get('button', 'left')
    if kind == 'click':
        pyautogui.click(action['x'], action['y'], button=button)
    elif kind == 'right_click':
        pyautogui.click(action['x'], action['y'], button='right')
    elif kind == 'double_click':
        pyautogui.doubleClick(action['x'], action['y'], button=button)
    elif kind == 'move':
        pyautogui.moveTo(action['x'], action['y'], duration=0.1)
    elif kind == 'drag':
        pts = [_pt(p) for p in action['path']]
        pyautogui.moveTo(*pts[0])
        pyautogui.mouseDown(button=button)
        for p in pts[1:]:
            pyautogui.moveTo(*p, duration=0.15)
        pyautogui.mouseUp(button=button)
    elif kind == 'scroll':
        pyautogui.moveTo(action['x'], action['y'])
        # pyautogui passes the raw wheel delta on Windows: one notch = 120
        notch = 120 if sys.platform == 'win32' else 1
        if action.get('scroll_y'):
            pyautogui.scroll(-int(action['scroll_y']) * notch)   # positive scroll_y = down
        if action.get('scroll_x'):
            pyautogui.hscroll(int(action['scroll_x']) * notch)
    elif kind == 'type':
        point = (action['x'], action['y']) if native_edit else None
        return _type_text(action['text'], expected_hwnd, point)
    elif kind == 'keypress':
        if expected_focus is not None:
            from win_text import foreground_focus
            if foreground_focus() != expected_focus:
                raise FocusLost('The original text field no longer has keyboard focus.')
        pyautogui.hotkey(*[map_key(k) for k in action['keys']])
    elif kind == 'wait':
        time.sleep(min(float(action.get('seconds', 1)), 10))


def _type_text(text: str, expected_hwnd: int, point: tuple[int, int] | None) -> TextDelivery:
    import pyautogui
    focus = None
    if sys.platform == 'win32':
        from win_text import foreground_focus, insert_multiline
        focus = foreground_focus()

    def guard() -> None:
        pyautogui.failSafeCheck()
        if focus is not None and (not focus[1] or (expected_hwnd and focus[0] != expected_hwnd)
                                  or foreground_focus() != focus):
            raise FocusLost('The original text field no longer has keyboard focus.')

    guard()
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if '\n' not in text:
        if focus is not None:
            _send_unicode(text, guard)
        else:
            pyautogui.write(text, interval=0.01)
        return TextDelivery('dispatched', focus)
    if focus is not None and point is not None and insert_multiline(text, focus, point, guard):
        return TextDelivery('verified', focus)

    # Multiline input never sends Enter. Unknown controls consume a paste shortcut.
    import pyperclip
    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        previous = None
    guard()
    pyperclip.copy(text)
    try:
        guard()
        try:
            pyautogui.hotkey('ctrl', 'v')
        except Exception as exc:
            raise TextDeliveryUnknown('The paste shortcut did not confirm text delivery.') from exc
        time.sleep(0.2)
    finally:
        # Respect a new clipboard value copied while the paste was running.
        with suppress(pyperclip.PyperclipException):
            if previous is not None and pyperclip.paste() == text:
                pyperclip.copy(previous)
    return TextDelivery('dispatched', focus)


def _send_unicode(text: str, guard: Callable[[], None]) -> None:
    import ctypes
    from ctypes import wintypes
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                    ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                    ('dwExtraInfo', ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                    ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                    ('dwExtraInfo', ctypes.c_size_t)]

    class _U(ctypes.Union):           # sized by MOUSEINPUT: correct on 32- and 64-bit
        _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('u', _U)]

    if not text:
        return
    data = text.encode('utf-16-le')
    events = (INPUT * len(data))()    # two events per two-byte UTF-16 code unit
    for i, (code,) in enumerate(struct.iter_unpack('<H', data)):
        for offset, flags in enumerate((KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)):
            event = events[2 * i + offset]
            event.type = 1
            event.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
    guard()
    sent = ctypes.windll.user32.SendInput(len(events), events, ctypes.sizeof(INPUT))
    if sent == 0:
        raise OSError('SendInput was blocked (target window may be elevated / UIPI)')
    if sent != len(events):
        raise TextDeliveryUnknown('SendInput queued only part of the text.')
