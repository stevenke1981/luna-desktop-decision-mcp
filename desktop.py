"""Screen capture, target annotation and (optional) action execution.

Coordinates are physical pixels of the primary monitor ("screen" space).
Screenshots may be downscaled; capture() reports the scale so callers that
work in image space can convert (screen = image * scale).
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time
from dataclasses import dataclass

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
    image: Image.Image   # possibly downscaled
    scale: float         # screen pixels per image pixel
    width: int           # screen width
    height: int          # screen height

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        return round(x * self.scale), round(y * self.scale)

    def to_image(self, x: float, y: float) -> tuple[float, float]:
        return x / self.scale, y / self.scale


def capture(max_width: int = MAX_WIDTH) -> Shot:
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[1]
        raw = sct.grab(mon)
    img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
    scale = 1.0
    if max_width and img.width > max_width:
        scale = img.width / max_width
        img = img.resize((max_width, round(img.height / scale)), Image.LANCZOS)
    return Shot(img, scale, raw.size[0], raw.size[1])


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
        x, y = shot.to_image(action['x'], action['y'])
        draw.ellipse((x - r, y - r, x + r, y + r), outline=(255, 0, 0), width=3)
        draw.line((x - 2 * r, y, x + 2 * r, y), fill=(255, 0, 0), width=2)
        draw.line((x, y - 2 * r, x, y + 2 * r), fill=(255, 0, 0), width=2)
    return img


def to_data_url(img: Image.Image, fmt: str = 'PNG') -> str:
    return f'data:image/{fmt.lower()};base64,' + base64.b64encode(to_bytes(img, fmt)).decode()


def to_bytes(img: Image.Image, fmt: str = 'PNG') -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


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


def execute(action: dict) -> None:
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
        if action.get('scroll_y'):
            pyautogui.scroll(-int(action['scroll_y']))   # positive scroll_y = down
        if action.get('scroll_x'):
            pyautogui.hscroll(int(action['scroll_x']))
    elif kind == 'type':
        _type_text(action['text'])
    elif kind == 'keypress':
        pyautogui.hotkey(*[map_key(k) for k in action['keys']])
    elif kind == 'wait':
        time.sleep(min(float(action.get('seconds', 1)), 10))


def _type_text(text: str) -> None:
    import pyautogui
    if text.isascii():
        pyautogui.write(text, interval=0.01)
        return
    # Non-ASCII (e.g. Chinese) cannot be typed key by key: paste via clipboard,
    # then restore the previous clipboard text.
    import pyperclip
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.2)
    if previous is not None:
        pyperclip.copy(previous)
