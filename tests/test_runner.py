"""Run a synthetic subprocess, never contact WorkBuddy or use a real session."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from portal.runner import classify, cleanup_stale_sessions, execute

SESSION = {'accessToken': 'synthetic-never-valid', 'uid': 'synthetic-user'}

FAKE_SCRIPT = '''import json, os, pathlib, sys
assert 'SECRET_KEY_FILE' not in os.environ
assert 'DATABASE_PASSWORD_FILE' not in os.environ
assert 'CREDENTIAL_KEYS_FILE' not in os.environ
assert 'PYTHONPATH' not in os.environ
session = json.loads(pathlib.Path(os.environ['WORKBUDDY_AUTH_FILE']).read_text())
assert session['auth']['accessToken'] == 'synthetic-never-valid'
assert session['auth']['endpoint'] == 'https://copilot.tencent.com'
action = sys.argv[1]
if action == 'all':
    print(json.dumps({'step': 'status', 'http': 200, 'needs_attention': False, 'body': {'sensitive': 'synthetic-never-valid'}}))
    print(json.dumps({'step': 'claim', 'http': 200, 'needs_attention': False, 'body': {'sensitive': 'synthetic-never-valid'}}))
elif action == 'status':
    print(json.dumps({'step': 'status', 'http': 200, 'needs_attention': False}))
else:
    assert action == 'auto'
    print(json.dumps({'result': 'CLAIMED', 'needs_attention': False, 'message': 'synthetic-never-valid'}))
'''

class RunnerIntegrationTests(SimpleTestCase):
    def test_bad_mode_or_trigger_never_starts_process(self):
        with patch('portal.runner.subprocess.Popen') as process:
            for mode, trigger in [('invalid', 'manual'), ('signin', 'invalid')]:
                with self.assertRaises(ValueError):
                    execute(SESSION, mode=mode, trigger=trigger)
            process.assert_not_called()

    def test_authentication_error_wins_over_http_200(self):
        result = classify(0, {'http': 200, 'result': 'AUTH_REJECTED'}, 'check')
        self.assertEqual(result[:2], ('failed', 'AUTH_REJECTED'))

    def test_debug_http_rejection_is_reported_without_raw_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'rejected_fixture.py'
            script.write_text("import json\nprint(json.dumps({'step': 'status', 'http': 401, 'body': 'synthetic-secret'}))\nprint(json.dumps({'step': 'claim', 'http': 401}))\n", encoding='utf-8')
            with override_settings(WORKBUDDY_SCRIPT=script, AUTH_TEMP_ROOT=root / 'auth'):
                for mode, trigger in [('signin', 'manual'), ('full', 'check')]:
                    result = execute(SESSION, mode=mode, trigger=trigger)
                    if trigger == 'check':
                        # Two JSON lines are invalid for the single status action.
                        self.assertEqual(result[1], 'INVALID_OUTPUT')
                    else:
                        self.assertEqual(result[:2], ('failed', 'AUTH_REJECTED'))
                    self.assertNotIn('synthetic-secret', str(result))
        self.assertEqual(classify(0, {'http': 401}, 'check')[:2], ('failed', 'AUTH_REJECTED'))
        self.assertEqual(classify(0, {'http': 403}, 'check')[:2], ('failed', 'FORBIDDEN'))

    def test_modes_temp_cleanup_minimal_env_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'upstream_fixture.py'
            script.write_text(FAKE_SCRIPT, encoding='utf-8')
            auth_dir = root / 'auth'
            with override_settings(WORKBUDDY_SCRIPT=script, AUTH_TEMP_ROOT=auth_dir), patch.dict('os.environ', {'SECRET_KEY_FILE': '/synthetic/secret', 'CREDENTIAL_KEYS_FILE': '/synthetic/keyring', 'DATABASE_PASSWORD_FILE': '/synthetic/db', 'PYTHONPATH': '/synthetic/path'}):
                for mode, trigger, expected in [('signin', 'manual', 'SIGNIN_OK'), ('full', 'manual', 'CLAIMED'), ('full', 'check', 'STATUS_OK')]:
                    with self.subTest(mode=mode, trigger=trigger):
                        result = execute(SESSION, mode=mode, trigger=trigger)
                        self.assertEqual(result[0], 'success', result)
                        self.assertEqual(result[1], expected)
                        self.assertNotIn('synthetic-never-valid', json.dumps(result))
                        self.assertEqual(list(auth_dir.iterdir()), [])
    def test_deadline_kills_and_removes_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'sleep_fixture.py'
            script.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
            auth_dir = root / 'auth'
            with override_settings(WORKBUDDY_SCRIPT=script, AUTH_TEMP_ROOT=auth_dir, WORKER_MAX_SECONDS=0):
                self.assertEqual(execute(SESSION, mode='full', trigger='manual')[1], 'PROCESS_LIMIT')
                self.assertEqual(list(auth_dir.iterdir()), [])
    def test_cleanup_only_removes_our_aged_directories(self):
        import os
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / 'task-old00001'
            current = root / 'task-new00001'
            unrelated = root / 'unrelated'
            for directory in (old, current, unrelated):
                directory.mkdir()
                (directory / 'session.json').write_text('synthetic-only', encoding='utf-8')
            os.utime(old, (time.time() - 1000, time.time() - 1000))
            os.utime(unrelated, (time.time() - 1000, time.time() - 1000))
            if os.name != 'nt':
                root.chmod(0o700)
            with override_settings(AUTH_TEMP_ROOT=root):
                self.assertEqual(cleanup_stale_sessions(), 1)
            self.assertFalse(old.exists())
            self.assertTrue(current.exists())
            self.assertTrue(unrelated.exists())
    def test_invalid_raw_output_is_not_propagated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'invalid_fixture.py'
            script.write_text("print('synthetic-secret-unexpected-output')\n", encoding='utf-8')
            with override_settings(WORKBUDDY_SCRIPT=script, AUTH_TEMP_ROOT=root / 'auth'):
                result = execute(SESSION, mode='full', trigger='manual')
                self.assertEqual(result[1], 'INVALID_OUTPUT')
                self.assertNotIn('synthetic', str(result))
