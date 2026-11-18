"""Offline tests: no network, no real mouse/keyboard. Run: python tests.py"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ['LUNA_AUDIT_LOG'] = 'off'

from PIL import Image

import decisions
import desktop
import server


def pred(name, p):
    return {'type': 'predicate', 'name': name, 'probability': p}


def verdict(choice, allow, confirm, deny, conf=0.9):
    return {'type': 'choice', 'name': 'verdict', 'choice': choice, 'confidence': conf,
            'probabilities': [{'value': 'allow', 'probability': allow},
                              {'value': 'confirm', 'probability': confirm},
                              {'value': 'deny', 'probability': deny}]}


def safe_answers(**over):
    ans = {'verdict': verdict('allow', 0.9, 0.08, 0.02),
           'goal_aligned': pred('goal_aligned', 0.95), 'irreversible': pred('irreversible', 0.05),
           'sensitive': pred('sensitive', 0.02), 'blocking_dialog': pred('blocking_dialog', 0.03),
           'risk': {'type': 'score', 'name': 'risk', 'score': 0.2},
           'target_visible': pred('target_visible', 0.9)}
    ans.update(over)
    return ans


T = decisions.Thresholds(0.5, 0.4, 0.4, 0.5, 0.5, 0.6, 0.5)
CLICK = {'type': 'click', 'x': 10, 'y': 10}


class PolicyTests(unittest.TestCase):
    def test_allow(self):
        self.assertEqual(decisions.apply_policy(safe_answers(), CLICK, T).verdict, 'allow')

    def test_deny_verdict(self):
        j = decisions.apply_policy(safe_answers(verdict=verdict('deny', .1, .2, .7)), CLICK, T)
        self.assertEqual(j.verdict, 'deny')

    def test_irreversible_needs_confirm(self):
        j = decisions.apply_policy(safe_answers(irreversible=pred('irreversible', .8)), CLICK, T)
        self.assertEqual(j.verdict, 'confirm')

    def test_target_missed_needs_confirm(self):
        j = decisions.apply_policy(safe_answers(target_visible=pred('target_visible', .2)), CLICK, T)
        self.assertEqual(j.verdict, 'confirm')

    def test_high_risk_needs_confirm(self):
        j = decisions.apply_policy(safe_answers(risk={'type': 'score', 'score': 2.4}), CLICK, T)
        self.assertEqual(j.verdict, 'confirm')

    def test_refusal_denies(self):
        j = decisions.apply_policy(safe_answers(sensitive={'type': 'refusal', 'name': 'sensitive'}),
                                   CLICK, T)
        self.assertEqual(j.verdict, 'deny')

    def test_target_question_only_for_pointer(self):
        names = [q['name'] for q in decisions.build_judge_questions({'type': 'type', 'text': 'x'})]
        self.assertNotIn('target_visible', names)
        self.assertIn('target_visible', [q['name'] for q in decisions.build_judge_questions(CLICK)])

    def test_request_shape(self):
        captured = {}

        def fake(input_items, questions, cache_key=None):
            captured.update(input=input_items, questions=questions)
            return {'answers': [dict(a, name=n) for n, a in safe_answers().items()], 'usage': {}}

        with mock.patch.object(decisions, 'call_decisions', fake):
            j = decisions.judge('save file', CLICK, 'data:image/png;base64,AA==')
        self.assertEqual(j.verdict, 'allow')
        parts = captured['input'][1]['content']
        self.assertEqual([p['type'] for p in parts], ['input_text', 'input_json', 'input_image'])
        choice = captured['questions'][0]
        self.assertEqual(choice['type'], 'choice')
        self.assertEqual([c['value'] for c in choice['choices']], ['allow', 'confirm', 'deny'])

    def test_choose_maps_index(self):
        payload = {'answers': [{'type': 'choice', 'name': 'best_action', 'choice': 'a1',
                                'confidence': .8, 'probabilities': []}]}
        with mock.patch.object(decisions, 'call_decisions', return_value=payload):
            r = decisions.choose('g', [CLICK, {'type': 'keypress', 'keys': ['ENTER']}], None)
        self.assertEqual(r['index'], 1)
        self.assertEqual(r['action']['type'], 'keypress')


class DesktopTests(unittest.TestCase):
    shot = desktop.Shot(Image.new('RGB', (800, 450)), 2.4, 1920, 1080)

    def test_scale_roundtrip(self):
        self.assertEqual(self.shot.to_screen(400, 225), (960, 540))
        a = server._to_screen({'type': 'drag', 'path': [[0, 0], [100, 50]]}, self.shot, 'image')
        self.assertEqual(a['path'], [[0, 0], [240, 120]])

    def test_validate(self):
        desktop.validate(CLICK, self.shot)
        for bad in [{'type': 'rm'}, {'type': 'click'}, {'type': 'click', 'x': 5000, 'y': 1},
                    {'type': 'keypress', 'keys': []}, {'type': 'drag', 'path': [[1, 1]]}]:
            with self.assertRaises(ValueError):
                desktop.validate(bad, self.shot)

    def test_annotate_ring_leaves_target_visible(self):
        img = desktop.annotate(self.shot, {'type': 'click', 'x': 960, 'y': 540})
        self.assertEqual(img.getpixel((400, 225)), (0, 0, 0))          # centre untouched
        ring = [img.getpixel((400 + dx, 225)) for dx in range(-40, 41)]
        self.assertIn((255, 0, 0), ring)

    def test_crop_keeps_screen_mapping(self):
        sub = self.shot.crop(480, 270, 1440, 810)       # screen rect -> image (200,112)..(600,337)
        self.assertEqual(sub.image.size, (400, 225))
        self.assertEqual(sub.to_screen(0, 0), (480, 269))
        self.assertEqual(sub.to_screen(200, 112.5), (960, 539))

    def test_key_map(self):
        self.assertEqual([desktop.map_key(k) for k in ['CTRL', 'ArrowLeft', 'a']],
                         ['ctrl', 'left', 'a'])


class ServerTests(unittest.TestCase):
    def test_perform_blocked_without_env(self):
        shot = desktop.Shot(Image.new('RGB', (100, 100)), 1.0, 100, 100)
        j = decisions.Judgement('allow', ['ok'])
        with mock.patch.dict(os.environ, {'LUNA_ALLOW_EXECUTE': '0'}), \
                mock.patch.object(desktop, 'capture', return_value=shot), \
                mock.patch.object(decisions, 'judge', return_value=j), \
                mock.patch.object(desktop, 'execute') as ex:
            out = server.perform_desktop_action('g', CLICK)
        ex.assert_not_called()
        self.assertIn('"executed": false', out[-1])

    def test_perform_confirm_requires_approval(self):
        shot = desktop.Shot(Image.new('RGB', (100, 100)), 1.0, 100, 100)
        j = decisions.Judgement('confirm', ['irreversible'])
        with mock.patch.dict(os.environ, {'LUNA_ALLOW_EXECUTE': '1'}), \
                mock.patch.object(desktop, 'capture', return_value=shot), \
                mock.patch.object(decisions, 'judge', return_value=j), \
                mock.patch.object(desktop, 'execute') as ex, \
                mock.patch.object(desktop, 'wait_stable'):
            server.perform_desktop_action('g', CLICK)
            ex.assert_not_called()
            server.perform_desktop_action('g', CLICK, user_approved=True)
            ex.assert_called_once()

    def test_token_auth(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})

        async def run(headers, qs=b''):
            sent = []

            async def send(m):
                sent.append(m)
            await server.TokenAuth(app, 'secret-token')(
                {'type': 'http', 'headers': headers, 'query_string': qs}, None, send)
            return sent[0]['status']

        self.assertEqual(asyncio.run(run([])), 401)
        self.assertEqual(asyncio.run(run([(b'authorization', b'Bearer secret-token')])), 200)
        self.assertEqual(asyncio.run(run([], b'token=secret-token')), 200)
        self.assertEqual(asyncio.run(run([(b'authorization', b'Bearer nope')])), 401)

    def test_tools_registered(self):
        names = {t.name for t in asyncio.run(server.server.list_tools())}
        self.assertEqual(names, {'observe', 'locate', 'next_step', 'act', 'focus_window'})


import agent_tools
import perception


def el(i, role, name, x, y):
    return perception.Element(f'e{i}', role, name, x, y, x - 10, y - 5, x + 10, y + 5)


class PerceptionTests(unittest.TestCase):
    def test_dedupe_link_button_pair(self):
        raw = [perception.Element('', 'link', 'Songs', 186, 413, 0, 400, 300, 426),
               perception.Element('', 'button', 'Songs', 186, 413, 0, 400, 300, 426),
               perception.Element('', 'textbox', '', 600, 413, 500, 400, 700, 426),
               perception.Element('', 'button', 'Search', 600, 413, 500, 400, 700, 426),
               perception.Element('', 'button', 'Close', 900, 20, 890, 10, 910, 30)]
        out = perception.dedupe(raw)
        self.assertEqual([(e.id, e.role, e.name) for e in out],
                         [('e1', 'button', 'Close'), ('e2', 'button', 'Songs'),
                          ('e3', 'textbox', 'Search')])

    def test_clean_strips_controls(self):
        self.assertEqual(perception.clean('ok\u202e\x07 now' + 'x' * 100)[:8], 'ok   now')
        self.assertEqual(len(perception.clean('x' * 100)), 60)

    def test_grid_cells(self):
        labels = perception.grid_labels(16, 9)
        self.assertEqual((len(labels), labels[0], labels[-1]), (144, 'A1', 'P9'))
        self.assertEqual(perception.cell_box(1, 2, 16, 9, 1600, 900), (100, 200, 200, 300))

    def test_marks_tag_outside_box(self):
        shot = desktop.Shot(Image.new('RGB', (800, 450)), 2.0, 1600, 900)
        img = perception.draw_marks(shot, [el(1, 'button', 'OK', 400, 400)])
        self.assertEqual(img.getpixel((195, 196)), (255, 0, 140))       # box edge
        self.assertEqual(img.getpixel((197, 190)), (255, 0, 140))       # tag above the box


WIN = perception.Window('App', 77, 0, 0, 1920, 1080)


def make_obs(*elements, window=WIN):
    shot = desktop.Shot(Image.new('RGB', (1600, 900)), 1.2, 1920, 1080)
    return agent_tools.Obs(shot, window, list(elements), False)


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        agent_tools.S.update(seq=0, current=None, obs={}, history=[], denied={}, approvals={})
        self.tools = {}
        fake = type('F', (), {'tool': lambda _s: (lambda f: self.tools.setdefault(f.__name__, f))})()
        agent_tools.register(fake, lambda: None, lambda *a, **k: None, lambda a: a)

    def test_name_match_needs_no_luna(self):
        ob = make_obs(el(1, 'button', 'Songs', 100, 100), el(2, 'button', 'Spaces', 100, 200))
        with mock.patch.object(decisions, 'call_decisions') as api:
            r = agent_tools._locate('the Songs button', ob)
        api.assert_not_called()
        self.assertEqual((r['element_id'], r['by']), ('e1', 'name_match'))

    def test_ambiguous_returns_candidates(self):
        ob = make_obs(el(1, 'button', 'Save', 100, 100), el(2, 'button', 'Save', 300, 100))
        pick = {'choice': 'e1', 'confidence': 0.5, 'refused': False,
                'top': [{'value': 'e1', 'probability': 0.5}, {'value': 'e2', 'probability': 0.45}]}
        with mock.patch.object(decisions, 'pick_element', return_value=pick):
            r = agent_tools._locate('Save', ob)
        self.assertEqual(r['code'], 'AMBIGUOUS')
        self.assertEqual([c['element_id'] for c in r['candidates']], ['e1', 'e2'])

    def test_grid_locate_maps_to_screen(self):
        ob = make_obs()
        answers = iter([{'visible': 0.9, 'col': 1, 'row': 2, 'confidence': 0.9, 'refused': False},
                        {'visible': 0.9, 'col': 3, 'row': 2, 'confidence': 0.8, 'refused': False}])
        with mock.patch.object(decisions, 'pick_grid', side_effect=lambda *a: next(answers)):
            r = agent_tools._locate_grid('thing', ob)
        # B3 = (100..200, 200..300); crop with margin (50..250, 150..350) zoomed 4x;
        # sub-cell D3 centre (350, 333.3) -> image (137.5, 233.3) -> screen x1.2
        self.assertEqual((r['x'], r['y'], r['by']), (165, 280, 'grid'))

    def test_tiers(self):
        ob = make_obs()
        gen, songs = el(1, 'button', 'Generate', 1, 1), el(2, 'link', 'Songs', 1, 1)
        known = {'by': 'element_id'}
        self.assertEqual(agent_tools._tier('click', gen, known, ob, None, None, False), 'risky')
        self.assertEqual(agent_tools._tier('click', songs, known, ob, None, None, False), 'low')
        self.assertEqual(agent_tools._tier('keypress', None, {}, ob, None, ['ESCAPE'], False), 'low')
        self.assertEqual(agent_tools._tier('keypress', None, {}, ob, None, ['ENTER'], False), 'risky')
        self.assertEqual(agent_tools._tier('click', songs, {'by': 'grid'}, ob, None, None, False), 'risky')
        term = make_obs(window=perception.Window('Windows PowerShell', 5, 0, 0, 10, 10))
        self.assertEqual(agent_tools._tier('scroll', None, {}, term, None, None, False), 'risky')

    def _act(self, elements, judge=None, **kw):
        state = {'ob': make_obs(*elements)}

        def observe(*a):
            state['ob'] = make_obs(*elements)
            return state['ob']
        j = mock.Mock(side_effect=lambda *a, **k: decisions.Judgement(judge or 'allow', ['x']))
        with mock.patch.object(agent_tools, '_observe', side_effect=observe), \
                mock.patch.object(decisions, 'judge', j), \
                mock.patch.object(perception, 'foreground_hwnd', return_value=77), \
                mock.patch.object(perception, 'hit_test', return_value={'hwnd': 77}), \
                mock.patch.object(desktop, 'wait_stable'), \
                mock.patch.object(desktop, 'execute') as ex:
            obs = make_obs(*elements).id
            out = self.tools['act']('goal', obs=obs, **kw)
        return json.loads(out[0]), ex, j

    def test_low_risk_click_skips_judge(self):
        res, ex, j = self._act([el(1, 'link', 'Songs', 300, 200)], element_id='e1')
        j.assert_not_called()
        ex.assert_called_once()
        self.assertEqual((res['status'], res['judge']['tier']), ('done', 'low'))
        self.assertEqual(res['elements'], ['e1 link "Songs" (300,200)'])

    def test_type_submit_clicks_types_enters(self):
        res, ex, j = self._act([el(1, 'textbox', 'Search', 300, 200)], judge='allow',
                               action='type', element_id='e1', text='hi', submit=True)
        j.assert_called_once()                      # submit makes it risky
        self.assertEqual([c.args[0]['type'] for c in ex.call_args_list], ['click', 'type', 'keypress'])

    def test_confirm_then_approval_id(self):
        gen = el(1, 'button', 'Generate', 300, 200)
        res, ex, _ = self._act([gen], judge='confirm', element_id='e1')
        ex.assert_not_called()
        self.assertEqual(res['error']['code'], 'NEEDS_APPROVAL')
        res2, ex2, j2 = self._act([gen], judge='confirm', element_id='e1',
                                  approval_id=res['approval_id'])
        j2.assert_not_called()
        ex2.assert_called_once()
        self.assertTrue(res2['executed'])

    def test_deny_is_sticky(self):
        gen = el(1, 'button', 'Delete all', 300, 200)
        res, ex, _ = self._act([gen], judge='deny', element_id='e1')
        self.assertEqual(res['error']['code'], 'DENIED')
        res2, ex2, j2 = self._act([gen], judge='allow', element_id='e1')
        j2.assert_not_called()
        ex2.assert_not_called()
        self.assertEqual(res2['error']['code'], 'DENIED')

    def test_stale_element(self):
        old = make_obs(el(1, 'button', 'Gone', 300, 200))
        with mock.patch.object(agent_tools, '_observe', return_value=make_obs()), \
                mock.patch.object(desktop, 'execute') as ex:
            res = json.loads(self.tools['act']('g', element_id='e1', obs=old.id)[0])
        ex.assert_not_called()
        self.assertEqual(res['error']['code'], 'STALE')

    def test_focus_lost(self):
        ob = make_obs(el(1, 'link', 'Songs', 300, 200))
        with mock.patch.object(agent_tools, '_observe', return_value=ob), \
                mock.patch.object(perception, 'foreground_hwnd', return_value=999), \
                mock.patch.object(desktop, 'execute') as ex:
            res = json.loads(self.tools['act']('g', element_id='e1', obs=ob.id)[0])
        ex.assert_not_called()
        self.assertEqual(res['error']['code'], 'FOCUS_LOST')

    def test_exec_disabled_spends_no_luna(self):
        tools = {}
        fake = type('F', (), {'tool': lambda _s: (lambda f: tools.setdefault(f.__name__, f))})()
        agent_tools.register(fake, lambda: 'disabled', lambda *a, **k: None, lambda a: a)
        ob = make_obs(el(1, 'button', 'Generate', 300, 200))
        with mock.patch.object(agent_tools, '_observe', return_value=ob), \
                mock.patch.object(decisions, 'call_decisions') as api:
            res = json.loads(tools['act']('g', element_id='e1', obs=ob.id)[0])
        api.assert_not_called()
        self.assertEqual(res['error']['code'], 'EXEC_DISABLED')

    def test_next_step_plan_shape(self):
        ob = make_obs(el(1, 'textbox', 'Prompt', 300, 200))
        r = {'choice': 'type:e1', 'confidence': 0.8, 'refused': False, 'expect': None,
             'top': [{'value': 'type:e1', 'probability': 0.8}, {'value': 'key:enter', 'probability': 0.1}],
             'state': {'goal_done': 0.1, 'loading': 0.0, 'blocking_dialog': 0.0}}
        with mock.patch.object(agent_tools, '_observe', return_value=ob), \
                mock.patch.object(decisions, 'next_step', return_value=r):
            plan = self.tools['next_step']('write a song')
        self.assertEqual(plan['decision'], 'act')
        self.assertTrue(plan['confident'])
        self.assertEqual(plan['suggestions'][0]['call']['args'],
                         {'obs': ob.id, 'goal': 'write a song', 'action': 'type',
                          'element_id': 'e1', 'text': '<TEXT>'})


if __name__ == '__main__':
    unittest.main()
