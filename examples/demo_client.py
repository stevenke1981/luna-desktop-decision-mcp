r"""Reference MCP client: focus_window -> observe -> locate -> next_step -> act.

Run from the repo root:  .venv\Scripts\python examples\demo_client.py
Needs OPENAI_API_KEY. It performs ONE real click (the suggested low-risk step),
so open the app first. Edit GOAL / WINDOW below for your own app.
"""
import asyncio, json, os, sys, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW = 'Flow Music'
GOAL = 'Open the Songs list in Flow Music to see the song generated earlier'


def show(name, res, t):
    txt = next(c.text for c in res.content if c.type == 'text')
    d = json.loads(txt)
    els = d.pop('elements', None)
    print(f'\n=== {name}  ({t:.2f}s)')
    print(json.dumps(d, ensure_ascii=False, indent=1)[:1800])
    if els:
        print(f'  elements: {len(els)}, e.g.', els[:3])
    return d


async def main():
    env = dict(os.environ, LUNA_ALLOW_EXECUTE='1', LUNA_AUDIT_LOG='off')
    p = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, 'server.py')], env=env)
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print('tools:', [t.name for t in (await s.list_tools()).tools])

            async def call(name, **args):
                t = time.time()
                res = await s.call_tool(name, args)
                if name == 'focus_window':
                    print(f'\n=== focus_window ({time.time() - t:.2f}s)', res.content[0].text[:300])
                    return None
                return show(name, res, time.time() - t)

            await call('focus_window', title_contains=WINDOW)
            await call('observe')
            await call('locate', target='Songs in the left sidebar')
            await call('locate', target='the play or pause button of the music player at the bottom')
            plan = await call('next_step', goal=GOAL)
            if plan['decision'] == 'act' and plan['suggestions']:
                args = plan['suggestions'][0]['call']['args']
                if args.get('action') == 'click':
                    await call('act', **args, expect='A list of songs is shown', plan_next=True)


asyncio.run(main())
