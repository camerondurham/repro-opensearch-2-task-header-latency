"""Real detached Thespian administrator + negative ownership cleanup tests."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from processes import owned_processes, stop_owned_processes
from run import cleanup_owned


class ProcessCleanupTests(unittest.TestCase):
    def test_detached_thespian_survives_launcher_but_owned_cleanup_removes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = dict(token=uuid.uuid4().hex, capture=temp, python=str(Path(sys.executable).resolve()),
                         started=time.time()-1)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, OSB_PROCESS_OWNER=owner['token'], OSB_CAPTURE=temp,
                       OSB_TEST_PORT=str(port), OSB_TEST_READY=str(Path(temp)/'ready'))
            # Do not reuse a global OSB actor system: this port/system is private.
            code = '''import os,time
from pathlib import Path
from thespian.actors import ActorSystem
system=ActorSystem('multiprocTCPBase', capabilities={'Admin Port':int(os.environ['OSB_TEST_PORT'])}, transientUnique=True)
Path(os.environ['OSB_TEST_READY']).write_text('ready')
while True: time.sleep(1)
'''
            with open(Path(temp)/'log', 'w') as log:
                child = subprocess.Popen([sys.executable, '-c', code], env=env, stdout=log, stderr=log,
                                         start_new_session=True)
                foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                    env={**env, 'OSB_PROCESS_OWNER': 'different-' + owner['token']}, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 20
                    while not Path(env['OSB_TEST_READY']).exists() and time.monotonic() < deadline and child.poll() is None:
                        time.sleep(.05)
                    self.assertTrue(Path(env['OSB_TEST_READY']).exists(), 'Private actor startup failed; see test log')
                    self.assertTrue(any(os.getsid(p.pid) != child.pid for p in owned_processes(owner)))
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(5)
                    self.assertTrue(owned_processes(owner), 'Regression must exercise detached live actors')
                    # Exercise the actual owner's cleanup path, not merely the helper.
                    with patch('run.command', return_value=''):
                        errors = cleanup_owned('test-owner', 'test-net', [], Path(temp), child, process_owner=owner)
                    self.assertEqual(errors, [])
                    self.assertEqual(owned_processes(owner), [])
                    self.assertIsNone(foreign.poll(), 'Cleanup touched a different owner')
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(5)
                    try:
                        stop_owned_processes(owner)
                    finally:
                        foreign.terminate()
                        foreign.wait(5)

    def test_identity_mismatch_is_not_killed(self):
        process = Mock()
        process.uids.return_value.real = os.getuid()
        process.create_time.return_value = time.time()
        process.environ.return_value = {'OSB_PROCESS_OWNER': 'ours', 'OSB_CAPTURE': 'different'}
        with patch('processes.psutil.process_iter', return_value=[process]):
            with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                stop_owned_processes(dict(token='ours', capture='expected', python='python', started=0))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_actor_cleanup_failure_cannot_report_clean(self):
        with tempfile.TemporaryDirectory() as temp, patch('run.command', return_value=''), \
                patch('run.stop_owned_processes', side_effect=RuntimeError('actors remain')):
            errors = cleanup_owned('owner', 'network', [], Path(temp), None, process_owner={})
        self.assertTrue(any('actors remain' in e for e in errors))
