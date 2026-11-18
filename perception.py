"""Local, fast perception: UI Automation elements, set-of-marks and grid overlays,
hit-testing and window focusing. No network calls here.

Coordinates are physical screen pixels (same space as desktop.execute).
"""
from __future__ import annotations

import contextlib
import re
import string
import sys
from dataclasses import dataclass, asdict

import desktop  # sets DPI awareness first
from PIL import Image, ImageDraw, ImageFont

INTERACTIVE = {
    'ButtonControl': 'button', 'EditControl': 'textbox', 'HyperlinkControl': 'link',
    'MenuItemControl': 'menuitem', 'CheckBoxControl': 'checkbox', 'ComboBoxControl': 'combobox',
    'ListItemControl': 'listitem', 'TabItemControl': 'tab', 'RadioButtonControl': 'radio',
    'SplitButtonControl': 'button', 'TreeItemControl': 'treeitem', 'SliderControl': 'slider',
    'DataItemControl': 'item',
}
ROLE_RANK = {'textbox': 0, 'combobox': 1, 'button': 2, 'link': 3}
_CTRL = re.compile(r'[\x00-\x1f\x7f​-‏‪-‮]')


def clean(text: str | None, limit: int = 60) -> str:
    """On-screen text is untrusted: strip control/bidi characters and cap the length."""
    return _CTRL.sub(' ', text or '').strip()[:limit]


@dataclass
class Element:
    id: str
    role: str
    name: str
    x: int            # center, screen px
    y: int
    left: int
    top: int
    right: int
    bottom: int
    value: str = ''
    focused: bool = False
    password: bool = False
    aid: str = ''     # AutomationId, used when the element has no name

    def label(self) -> str:
        name = self.name or (f'id={self.aid}' if self.aid else '(unnamed)')
        out = f'{self.role} "{name}"'
        if self.value:
            out += f' value="{self.value}"'
        if self.password:
            out += ' password'
        if self.focused:
            out += ' focused'
        return out

    def fingerprint(self) -> tuple:
        return (self.role, self.name, self.aid, self.x // 24, self.y // 24)

    def as_dict(self) -> dict:
        return asdict(self)


def _font(size: int):
    for f in ('arial.ttf', 'msjh.ttc', 'segoeui.ttf'):
        with contextlib.suppress(OSError):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------- UIA

def _auto():
    """Import uiautomation with stdout redirected: comtypes prints while building
    its cache, which would corrupt the MCP stdio stream."""
    with contextlib.redirect_stdout(sys.stderr):
        import uiautomation as auto
    return auto


def foreground_window():
    auto = _auto()
    ctl = auto.GetForegroundControl()
    top = ctl.GetTopLevelControl() if ctl else None
    return top or ctl


@dataclass
class Window:
    title: str
    hwnd: int
    left: int
    top: int
    right: int
    bottom: int


def scan(screen_w: int, screen_h: int, max_elements: int = 150,
         max_depth: int = 40) -> tuple[Window | None, list[Element], bool]:
    """Visible, enabled interactive elements of the foreground window.
    Returns (window, elements, truncated)."""
    auto = _auto()
    with auto.UIAutomationInitializerInThread():
        win = foreground_window()
        if win is None:
            return None, [], False
        r = win.BoundingRectangle
        window = Window(clean(win.Name, 120), int(win.NativeWindowHandle or 0),
                        r.left, r.top, r.right, r.bottom)
        raw = _walk(auto, win, screen_w, screen_h, max_depth)
        if len(raw) < 15 and 'Chrome' in (win.ClassName or '') + window.title:
            import time                           # Chrome builds its tree lazily
            time.sleep(0.3)
            raw = _walk(auto, win, screen_w, screen_h, max_depth)
    kept = dedupe(raw)
    return window, kept[:max_elements], len(kept) > max_elements


def _walk(auto, win, screen_w, screen_h, max_depth) -> list[Element]:
    raw: list[Element] = []
    for c, _ in auto.WalkControl(win, includeTop=False, maxDepth=max_depth):
        role = INTERACTIVE.get(c.ControlTypeName)
        if not role:
            continue
        try:
            r = c.BoundingRectangle
            if r.width() <= 2 or r.height() <= 2 or c.IsOffscreen or not c.IsEnabled:
                continue
            cx, cy = r.xcenter(), r.ycenter()
            if not (0 <= cx < screen_w and 0 <= cy < screen_h):
                continue
            e = Element('', role, clean(c.Name), cx, cy, r.left, r.top, r.right, r.bottom,
                        aid=clean(c.AutomationId, 40))
            if role == 'textbox':
                e.password = bool(getattr(c, 'IsPassword', False))
                if not e.password:
                    with contextlib.suppress(Exception):
                        e.value = clean(c.GetValuePattern().Value, 40)
            with contextlib.suppress(Exception):
                e.focused = bool(c.HasKeyboardFocus)
        except Exception:
            continue
        raw.append(e)
        if len(raw) >= 1500:
            break
    return raw


def dedupe(elements: list[Element], tol: int = 8) -> list[Element]:
    """Drop near-identical duplicates (Chrome reports link+button pairs) and number
    the rest in reading order."""
    kept: list[Element] = []
    for e in sorted(elements, key=lambda e: (ROLE_RANK.get(e.role, 9), not e.name)):
        dup = next((k for k in kept if abs(e.x - k.x) <= tol and abs(e.y - k.y) <= tol and
                    (e.name == k.name or not e.name or not k.name)), None)
        if dup:
            if not dup.name and e.name:
                dup.name = e.name                 # keep the better role, borrow the name
            continue
        kept.append(e)
    kept.sort(key=lambda e: (round(e.y / 16), e.x))
    for i, e in enumerate(kept, 1):
        e.id = f'e{i}'
    return kept


def hit_test(x: int, y: int) -> dict:
    """What UI Automation reports under a screen point (role, name, top-level window)."""
    auto = _auto()
    try:
        with auto.UIAutomationInitializerInThread():
            c = auto.ControlFromPoint(x, y)
            if not c:
                return {}
            top = c.GetTopLevelControl()
            return {'role': INTERACTIVE.get(c.ControlTypeName, c.ControlTypeName),
                    'name': clean(c.Name), 'window': clean(top.Name if top else '', 120),
                    'hwnd': int(top.NativeWindowHandle or 0) if top else 0}
    except Exception:
        return {}


# ---------------------------------------------------------------- overlays

def draw_marks(shot: 'desktop.Shot', elements: list[Element]) -> Image.Image:
    """Set-of-marks: box per element, number tag just outside its top-left corner."""
    img = shot.image.copy()
    d = ImageDraw.Draw(img)
    font = _font(max(12, img.width // 110))
    for e in elements:
        l, t = shot.to_image(e.left, e.top)
        r, b = shot.to_image(e.right, e.bottom)
        if r < 0 or b < 0 or l > img.width or t > img.height:
            continue
        d.rectangle((l, t, r, b), outline=(255, 0, 140), width=2)
        tag = e.id[1:]
        tw, th = d.textlength(tag, font=font) + 6, font.size + 4
        ty = t - th if t - th >= 0 else b                     # above the box, else below
        d.rectangle((l, ty, l + tw, ty + th), fill=(255, 0, 140))
        d.text((l + 3, ty + 1), tag, fill='white', font=font)
    return img


def grid_labels(cols: int, rows: int) -> list[str]:
    letters = string.ascii_uppercase
    return [f'{letters[c]}{r + 1}' for r in range(rows) for c in range(cols)]


def draw_grid(img: Image.Image, cols: int, rows: int) -> Image.Image:
    """Yellow grid with column letters along the top and row numbers down the left."""
    img = img.copy()
    d = ImageDraw.Draw(img)
    cw, ch = img.width / cols, img.height / rows
    font = _font(max(12, int(min(cw, ch) / 3.5)))
    for c in range(1, cols):
        d.line((c * cw, 0, c * cw, img.height), fill=(255, 220, 0), width=2)
    for r in range(1, rows):
        d.line((0, r * ch, img.width, r * ch), fill=(255, 220, 0), width=2)
    for c in range(cols):
        d.text((c * cw + cw / 2 - 5, 2), string.ascii_uppercase[c], fill=(255, 220, 0),
               font=font, stroke_width=3, stroke_fill='black')
    for r in range(rows):
        d.text((3, r * ch + ch / 2 - 8), str(r + 1), fill=(255, 220, 0), font=font,
               stroke_width=3, stroke_fill='black')
    return img


def cell_box(col: int, row: int, cols: int, rows: int, w: float, h: float):
    cw, ch = w / cols, h / rows
    return col * cw, row * ch, (col + 1) * cw, (row + 1) * ch


# ---------------------------------------------------------------- windows

def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = wintypes.HWND
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    return u


def list_windows() -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes
    u = _user32()
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        n = u.GetWindowTextLengthW(h)
        if n and u.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            out.append((int(h), b.value))
        return True
    u.EnumWindows(cb, 0)
    return out


def _own_console() -> int:
    import ctypes
    with contextlib.suppress(Exception):
        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    return 0


def foreground_hwnd() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def focus_window(title_contains: str) -> dict:
    """Bring the best-matching visible window to the front (exact title first;
    this server's own console is never chosen)."""
    if sys.platform != 'win32':
        raise RuntimeError('focus_window is Windows-only')
    import ctypes
    import time
    u = _user32()
    k32 = ctypes.windll.kernel32
    q = title_contains.lower()
    own = _own_console()
    wins = [(h, t) for h, t in list_windows() if h != own]
    matches = sorted((w for w in wins if q in w[1].lower()),
                     key=lambda w: (w[1].lower() != q, not w[1].lower().startswith(q), len(w[1])))
    if not matches:
        return {'ok': False, 'error': f'no window title contains {title_contains!r}',
                'windows': [t for _, t in wins][:30]}
    h, title = matches[0]
    if u.IsIconic(h):
        u.ShowWindow(h, 9)                                   # SW_RESTORE
    fg = u.GetForegroundWindow()
    fg_thread = u.GetWindowThreadProcessId(fg, None) if fg else 0
    me = k32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and u.AttachThreadInput(me, fg_thread, True))
    try:
        u.BringWindowToTop(h)
        u.SetForegroundWindow(h)
    finally:
        if attached:
            u.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.3)
    ok = int(u.GetForegroundWindow() or 0) == h
    return {'ok': ok, 'title': title, 'hwnd': h,
            'other_matches': [t for _, t in matches[1:6]]}
