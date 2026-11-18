"""Luna Desktop Decision MCP.

Lets any MCP agent (Codex, ChatGPT desktop, Claude Code, ...) ask OpenAI's
Decisions API (gpt-6-luna) whether a proposed desktop operation should run,
which of several candidate operations to take, or whether conditions hold on
the current screen. Optionally executes approved operations locally.

Run:  python server.py                      # stdio (Codex, Claude Code, Cursor...)
      python server.py --http --port 8765   # Streamable HTTP at /mcp (ChatGPT)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

import decisions
import desktop

server = FastMCP('luna-desktop-decision')

STATE = {'http': False}
AUDIT_PATH = Path(os.environ.get(
    'LUNA_AUDIT_LOG', Path.home() / '.luna-desktop-decision' / 'audit.jsonl'))


def _audit(event: str, **data: Any) -> None:
    if os.environ.get('LUNA_AUDIT_LOG', '') == 'off':
        return
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': time.time(), 'event': event, **data},
                               ensure_ascii=False) + '\n')
    except OSError:
        pass


def _redact(action: dict) -> dict:
    a = dict(action)
    if 'text' in a:
        a['text'] = f'<{len(a["text"])} chars>'
    return a


def _to_screen(action: dict, shot: desktop.Shot, space: str) -> dict:
    if space == 'screen':
        return dict(action)
    if space != 'image':
        raise ValueError("coordinate_space must be 'screen' or 'image'")
    a = dict(action)
    if a.get('x') is not None and a.get('y') is not None:
        a['x'], a['y'] = shot.to_screen(a['x'], a['y'])
    if a.get('path'):
        a['path'] = [list(shot.to_screen(*desktop._pt(p))) for p in a['path']]
    return a


def _execution_enabled() -> str | None:
    if os.environ.get('LUNA_ALLOW_EXECUTE') != '1':
        return 'execution disabled: set LUNA_ALLOW_EXECUTE=1 in the server environment'
    if STATE['http'] and os.environ.get('LUNA_HTTP_ALLOW_EXECUTE') != '1':
        return 'execution over HTTP disabled: also set LUNA_HTTP_ALLOW_EXECUTE=1'
    return None


@server.tool()
def capture_screen(max_width: int = desktop.MAX_WIDTH) -> list:
    """Screenshot of the primary monitor. Returns the image and its metadata.

    Coordinates in the image are 'image' space; screen = image * scale. Pass
    coordinate_space='image' to the other tools to use image coordinates directly.
    """
    shot = desktop.capture(max_width)
    meta = {'image_width': shot.image.width, 'image_height': shot.image.height,
            'screen_width': shot.width, 'screen_height': shot.height, 'scale': shot.scale}
    return [Image(data=desktop.to_bytes(shot.image), format='png'), json.dumps(meta)]


@server.tool()
def judge_desktop_action(goal: str, action: dict, coordinate_space: str = 'screen',
                         history: list[str] | None = None) -> dict:
    """Ask gpt-6-luna (OpenAI Decisions API) whether a desktop action should run now.

    Call this BEFORE performing any click/type/keypress on the user's desktop,
    whatever tool you use to perform it. A fresh screenshot with the target
    marked is sent to OpenAI (billable, leaves the machine).

    action: {"type": click|double_click|right_click|move|drag|scroll|type|keypress|wait,
             "x","y" (pointer actions), "button", "path" [[x,y],...] (drag),
             "scroll_x","scroll_y", "text" (type), "keys" ["CTRL","S"] (keypress),
             "description": what element/intent this is (strongly recommended)}
    history: short descriptions of recent actions already taken.

    Returns verdict 'allow' | 'confirm' (ask the human first) | 'deny', reasons,
    and the raw typed answers (probabilities). Do not proceed on 'deny'.
    """
    shot = desktop.capture()
    a = _to_screen(action, shot, coordinate_space)
    desktop.validate(a, shot)
    img = desktop.to_data_url(desktop.annotate(shot, a))
    result = decisions.judge(goal, a, img, history).as_dict()
    _audit('judge', goal=goal, action=_redact(a), verdict=result['verdict'],
           reasons=result['reasons'])
    return result


@server.tool()
def choose_desktop_action(goal: str, candidates: list[dict],
                          coordinate_space: str = 'screen') -> dict:
    """Ask gpt-6-luna which candidate action is the best next step on the current screen.

    candidates: 1-254 action objects (same schema as judge_desktop_action).
    Returns choice ('aN' or 'none'), index, the chosen action, confidence and
    probabilities. The chosen action still needs judge/perform before running.
    """
    if not 1 <= len(candidates) <= 254:
        raise ValueError('candidates must contain 1 to 254 actions')
    shot = desktop.capture()
    cands = [_to_screen(c, shot, coordinate_space) for c in candidates]
    result = decisions.choose(goal, cands, desktop.to_data_url(shot.image))
    _audit('choose', goal=goal, choice=result['choice'], count=len(cands))
    return result


@server.tool()
def check_screen(conditions: list[str], context: str | None = None) -> dict:
    """Evaluate yes/no conditions about the current screen with gpt-6-luna.

    e.g. ["The Save As dialog is open", "An error message is visible"].
    Returns a probability (0-1) per condition. Use it to verify an action worked.
    """
    if not 1 <= len(conditions) <= 400:
        raise ValueError('conditions must contain 1 to 400 items')
    shot = desktop.capture()
    return decisions.check_conditions(conditions, desktop.to_data_url(shot.image), context)


@server.tool()
def perform_desktop_action(goal: str, action: dict, coordinate_space: str = 'screen',
                           user_approved: bool = False,
                           history: list[str] | None = None) -> list:
    """Judge an action with gpt-6-luna and execute it locally only if permitted.

    - 'allow'   -> executed.
    - 'confirm' -> executed only when user_approved=true. Set user_approved ONLY after
                   the human explicitly approved this exact action in the conversation.
    - 'deny'    -> never executed.
    Requires LUNA_ALLOW_EXECUTE=1 on the server. Abort any time by moving the
    mouse into a screen corner (PyAutoGUI failsafe). Returns the judgement and
    a screenshot taken after the action.
    """
    blocked = _execution_enabled()
    shot = desktop.capture()
    a = _to_screen(action, shot, coordinate_space)
    desktop.validate(a, shot)
    j = decisions.judge(goal, a, desktop.to_data_url(desktop.annotate(shot, a)), history)
    result = j.as_dict()
    confirm_policy = os.environ.get('LUNA_CONFIRM_POLICY', 'approve')  # approve | never
    run = (j.verdict == 'allow' or
           (j.verdict == 'confirm' and user_approved and confirm_policy != 'never'))
    if blocked:
        result.update(executed=False, note=blocked)
    elif not run:
        result.update(executed=False, note='not executed: ' + (
            'denied' if j.verdict == 'deny' else 'needs explicit user approval'))
    else:
        desktop.execute(a)
        time.sleep(float(os.environ.get('LUNA_SETTLE_SECONDS', '0.6')))
        result['executed'] = True
    _audit('perform', goal=goal, action=_redact(a), verdict=j.verdict,
           user_approved=user_approved, executed=result['executed'])
    out: list = [json.dumps(result, ensure_ascii=False)]
    if result['executed']:
        out.insert(0, Image(data=desktop.to_bytes(desktop.capture().image), format='png'))
    return out


class TokenAuth:
    """ASGI middleware: require Authorization: Bearer <token> or ?token=<token>."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope.get('headers') or [])
            auth = headers.get(b'authorization', b'')
            supplied = auth[7:] if auth.lower().startswith(b'bearer ') else b''
            if not supplied:
                from urllib.parse import parse_qs
                q = parse_qs(scope.get('query_string', b'').decode())
                supplied = (q.get('token') or [''])[0].encode()
            if not hmac.compare_digest(supplied, self.token):
                await send({'type': 'http.response.start', 'status': 401,
                            'headers': [(b'content-type', b'text/plain')]})
                await send({'type': 'http.response.body', 'body': b'unauthorized'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', action='store_true', help='serve Streamable HTTP at /mcp')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args()
    if not args.http:
        server.run(transport='stdio')
        return
    token = os.environ.get('LUNA_MCP_TOKEN')
    if not token or len(token) < 24:
        raise SystemExit('Set LUNA_MCP_TOKEN (>= 24 random chars) before using --http')
    STATE['http'] = True
    import uvicorn
    uvicorn.run(TokenAuth(server.streamable_http_app(), token), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
