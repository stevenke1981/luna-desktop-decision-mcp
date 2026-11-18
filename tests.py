"""Offline tests: no network, no real mouse/keyboard. Run: python tests.py"""
import asyncio
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

    def test_annotate_marks_target(self):
        img = desktop.annotate(self.shot, {'type': 'click', 'x': 960, 'y': 540})
        self.assertEqual(img.getpixel((400, 225)), (255, 0, 0))

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
                mock.patch.object(server.time, 'sleep'):
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
        self.assertEqual(names, {'capture_screen', 'judge_desktop_action', 'choose_desktop_action',
                                 'check_screen', 'perform_desktop_action'})


if __name__ == '__main__':
    unittest.main()
