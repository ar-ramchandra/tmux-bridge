"""Exercise history against an isolated tmux server; never touch the user's socket."""
import ast
import pathlib
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]


class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bridge-history-test-')
        cls.socket = str(pathlib.Path(cls.temp.name) / 'tmux.sock')
        def run(args, **kwargs):
            return subprocess.run(args[:1] + ['-S', cls.socket] + args[1:], **kwargs)
        cls.tmux_run = staticmethod(run)
        tree = ast.parse((ROOT / 'server.py').read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('_rpc_history', '_rpc_scroll')]
        namespace = {'subprocess': SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'server.py', 'exec'), namespace)
        cls.history = staticmethod(namespace['_rpc_history'])
        cls.scroll = staticmethod(namespace['_rpc_scroll'])
        run(['tmux', 'new-session', '-d', '-s', 'history-test', '-x', '80', '-y', '24',
             "i=0; while [ $i -lt 100 ]; do printf 'marker-%s\\n' \"$i\"; i=$((i+1)); done; sleep 120"], check=True)
        for _ in range(40):
            result = cls.history({'session': 'history-test'})
            if 'marker-99' in result.get('body', {}).get('text', ''):
                break
            time.sleep(.05)

    @classmethod
    def tearDownClass(cls):
        cls.tmux_run(['tmux', 'kill-server'], capture_output=True)
        cls.temp.cleanup()

    def test_read_history_has_old_and_current_output(self):
        result = self.history({'session': 'history-test'})
        self.assertEqual(result['status'], 200)
        self.assertIn('marker-0', result['body']['text'])
        self.assertIn('marker-99', result['body']['text'])

    def test_return_live_only_exits_copy_mode(self):
        self.tmux_run(['tmux', 'copy-mode', '-t', '=history-test:'], check=True)
        self.assertEqual(self.history({'session': 'history-test'}, True)['status'], 200)
        mode = self.tmux_run(['tmux', 'display-message', '-p', '-t', '=history-test:', '#{pane_mode}'], capture_output=True, text=True)
        self.assertEqual(mode.stdout.strip(), '')
        self.assertEqual(self.history({'session': 'history-test'}, True)['status'], 200)
        self.assertIn('marker-99', self.history({'session': 'history-test'})['body']['text'])

    def test_scroll_without_mouse_enters_tmux_history(self):
        self.history({'session': 'history-test'}, True)
        result = self.scroll({'session': 'history-test', 'lines': -8, 'col': 10, 'row': 5})
        self.assertEqual(result['status'], 200)
        self.assertTrue(result['body']['copy_mode'])
        mode = self.tmux_run(['tmux', 'display-message', '-p', '-t', '=history-test:', '#{pane_mode}'], capture_output=True, text=True)
        self.assertEqual(mode.stdout.strip(), 'copy-mode')
        self.assertEqual(self.history({'session': 'history-test', 'pane': result['body']['pane']}, True)['status'], 200)

    def test_scroll_rejects_invalid_target_and_amount(self):
        self.assertEqual(self.scroll({'session': 'missing', 'lines': -3})['status'], 404)
        self.assertEqual(self.scroll({'session': 'history-test', 'lines': 'not-a-number'})['status'], 400)
        self.assertEqual(self.scroll({'session': 'history-test', 'lines': -3, 'col': 10000})['status'], 400)

    def test_missing_session(self):
        self.assertEqual(self.history({'session': 'missing'})['status'], 404)

    def test_invalid_request(self):
        self.assertEqual(self.history({})['status'], 400)
        self.assertEqual(self.history({'session': []})['status'], 400)


if __name__ == '__main__':
    unittest.main()
