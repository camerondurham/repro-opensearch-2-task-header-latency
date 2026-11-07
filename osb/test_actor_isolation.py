"""Pinned native OSB lifecycle regression tests; no engine or benchmark workload."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from osbenchmark import actor, benchmark, config
from thespian.actors import Actor

from processes import owned_processes, stop_owned_processes


CAPTURE = Path(__file__).resolve().parent / 'capture'


def load_capture_file(name):
    # Match external direct-file imports without putting capture on sys.path.
    spec = importlib.util.spec_from_file_location('capture_test_' + name, CAPTURE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ActorIsolationTests(unittest.TestCase):
    def setUp(self):
        # The adapter intentionally patches process globals in OSB. Never let a
        # test import/install change subsequent tests in this interpreter.
        self.enterContext(patch.object(actor, '__SYSTEM_BASE', 'multiprocTCPBase'))
        self.enterContext(patch.object(actor, 'actor_system_already_running', actor.actor_system_already_running))

    def simulate_bootstrap_race(self, isolated, fail_runnable=False):
        """Run *native* with_actor_system and bootstrap; fake only TCP and Thespian.

        No admin exists at the initial probe. A foreign one appears exactly as
        ActorSystem starts, so native TCP bootstrap adopts it despite try_join=False.
        """
        foreign_running = False
        foreign = Mock(name='foreign_admin')
        own = Mock(name='owned_queue_admin')

        def connect(address):
            self.assertEqual(address, ('127.0.0.1', 1900))
            if not foreign_running:
                raise ConnectionRefusedError('no admin at initial check')

        def shutdown_foreign():
            nonlocal foreign_running
            foreign_running = False

        def bootstrap(base, **kwargs):
            nonlocal foreign_running
            # This is the race, not a pre-existing service discovered by a probe.
            foreign_running = True
            self.assertEqual(kwargs['capabilities']['coordinator'], True)
            return own if base == 'multiprocQueueBase' else foreign

        foreign.shutdown.side_effect = shutdown_foreign
        probe_socket = Mock()
        probe_socket.connect.side_effect = connect
        native_lifecycle = benchmark.with_actor_system
        native_bootstrap = actor.bootstrap_actor_system
        runnable = Mock(side_effect=ValueError('runnable failed') if fail_runnable else None)
        cfg = config.Config()
        with patch.object(actor.socket, 'socket', return_value=probe_socket) as sockets, \
                patch.object(actor.thespian.actors, 'ActorSystem', side_effect=bootstrap) as factory, \
                patch.object(actor.log, 'load_configuration', return_value={}), \
                patch.object(benchmark.time, 'sleep'):
            if isolated:
                load_capture_file('actor_isolation').install()
            self.assertIs(benchmark.with_actor_system, native_lifecycle)
            self.assertIs(actor.bootstrap_actor_system, native_bootstrap)
            if fail_runnable:
                with self.assertRaisesRegex(ValueError, 'runnable failed'):
                    benchmark.with_actor_system(runnable, cfg)
            else:
                benchmark.with_actor_system(runnable, cfg)
            runnable.assert_called_once_with(cfg)
            self.assertIs(cfg.opts('system', 'remote.benchmarking.supported'), False)
        return factory, sockets, own, foreign, foreign_running

    def test_native_tcp_false_check_can_shutdown_foreign_admin(self):
        factory, sockets, own, foreign, running = self.simulate_bootstrap_race(isolated=False)
        self.assertEqual(factory.call_args.args, ('multiprocTCPBase',))
        self.assertEqual(sockets.call_count, 2)  # startup and post-shutdown probes
        foreign.shutdown.assert_called_once_with()
        own.shutdown.assert_not_called()
        self.assertFalse(running)  # Regression control: the unmodified TCP path is unsafe.

    def test_adapter_never_probes_or_joins_foreign_admin_and_shuts_down_own(self):
        factory, sockets, own, foreign, running = self.simulate_bootstrap_race(isolated=True)
        factory.assert_called_once_with('multiprocQueueBase', logDefs={}, capabilities={'coordinator': True})
        sockets.assert_not_called()
        own.shutdown.assert_called_once_with()
        foreign.shutdown.assert_not_called()
        self.assertTrue(running)

    def test_native_finally_shuts_down_only_own_queue_on_runnable_failure(self):
        _, sockets, own, foreign, running = self.simulate_bootstrap_race(isolated=True, fail_runnable=True)
        sockets.assert_not_called()
        own.shutdown.assert_called_once_with()
        foreign.shutdown.assert_not_called()
        self.assertTrue(running)

    def test_import_is_inert_and_install_uses_official_queue_selector(self):
        probe = actor.actor_system_already_running
        adapter = load_capture_file('actor_isolation')
        self.assertEqual(getattr(actor, '__SYSTEM_BASE'), 'multiprocTCPBase')
        self.assertIs(actor.actor_system_already_running, probe)
        with patch.object(actor, 'use_offline_actor_system', wraps=actor.use_offline_actor_system) as select:
            adapter.install()
        select.assert_called_once_with()
        self.assertEqual(getattr(actor, '__SYSTEM_BASE'), 'multiprocQueueBase')
        with patch.object(actor.socket, 'socket') as sockets:
            self.assertIs(actor.actor_system_already_running(), False)
            self.assertIs(actor.actor_system_already_running(ip='203.0.113.1'), False)
            sockets.assert_not_called()

    def test_probe_fails_closed_if_base_changes_or_disappears(self):
        load_capture_file('actor_isolation').install()
        for base in ('multiprocTCPBase', 'multiprocUDPBase', 'simpleSystemBase', None):
            with self.subTest(base=base), patch.object(actor, '__SYSTEM_BASE', base), \
                    patch.object(actor.thespian.actors, 'ActorSystem') as factory, \
                    patch.object(actor.socket, 'socket') as sockets:
                with self.assertRaisesRegex(RuntimeError, 'multiprocQueueBase'):
                    benchmark.with_actor_system(Mock(), config.Config())
                factory.assert_not_called()
                sockets.assert_not_called()
        delattr(actor, '__SYSTEM_BASE')
        with self.assertRaisesRegex(RuntimeError, 'multiprocQueueBase'):
            actor.actor_system_already_running()

    def test_install_rejects_selector_that_does_not_select_queue(self):
        probe = actor.actor_system_already_running
        with patch.object(actor, 'use_offline_actor_system'), \
                self.assertRaisesRegex(RuntimeError, 'multiprocQueueBase'):
            load_capture_file('actor_isolation').install()
        self.assertIs(actor.actor_system_already_running, probe)

    def test_version_guards_precede_any_global_patch(self):
        for package, bad in (('opensearch-benchmark', '2.5.1'), ('thespian', '3.10.7')):
            with self.subTest(package=package):
                adapter = load_capture_file('actor_isolation')
                versions = {'opensearch-benchmark': '2.5.0', 'thespian': '3.10.6', package: bad}
                probe = actor.actor_system_already_running
                with patch.object(adapter, 'version', side_effect=versions.__getitem__), \
                        patch.object(actor, 'use_offline_actor_system') as select, \
                        self.assertRaisesRegex(RuntimeError, package):
                    adapter.install()
                select.assert_not_called()
                self.assertIs(actor.actor_system_already_running, probe)
                self.assertEqual(getattr(actor, '__SYSTEM_BASE'), 'multiprocTCPBase')

    def test_native_offline_fallback_also_never_probes_tcp(self):
        load_capture_file('actor_isolation').install()
        own = Mock()
        with patch.object(actor.thespian.actors, 'ActorSystem',
                          side_effect=[actor.thespian.actors.InvalidActorAddress('test', 'test'), own]) as factory, \
                patch.object(actor.socket, 'socket') as sockets, \
                patch.object(actor.log, 'load_configuration', return_value={}), \
                patch.object(benchmark.time, 'sleep'):
            benchmark.with_actor_system(Mock(), config.Config())
        self.assertEqual([call.args for call in factory.call_args_list], [('multiprocQueueBase',)] * 2)
        sockets.assert_not_called()
        own.shutdown.assert_called_once_with()


class CaptureStartupTests(unittest.TestCase):
    def test_automatic_capture_install_precedes_benchmark_import_and_is_opt_in(self):
        code = '''import json, sys
before = 'osbenchmark.benchmark' in sys.modules
actor_loaded = 'osbenchmark.actor' in sys.modules
from osbenchmark import actor
print(json.dumps({'benchmark_loaded': before, 'actor_loaded': actor_loaded,
                  'base': getattr(actor, '__SYSTEM_BASE')}))
'''
        with tempfile.TemporaryDirectory() as temp:
            for enabled in (False, True):
                env = dict(os.environ, PYTHONPATH=str(CAPTURE))
                env.pop('OSB_CAPTURE', None)
                if enabled:
                    env['OSB_CAPTURE'] = temp
                result = subprocess.run([sys.executable, '-c', code], env=env,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {
                    'benchmark_loaded': False, 'actor_loaded': enabled,
                    'base': 'multiprocQueueBase' if enabled else 'multiprocTCPBase'})

    def test_sitecustomize_version_failure_exits_closed_before_metrics_patch(self):
        from osbenchmark import metrics
        for package, bad in (('opensearch-benchmark', '2.6.0'), ('thespian', '3.10.5')):
            with self.subTest(package=package), tempfile.TemporaryDirectory() as temp, \
                    patch.dict(os.environ, OSB_CAPTURE=temp), \
                    patch.object(actor, '__SYSTEM_BASE', 'multiprocTCPBase'), \
                    patch.object(actor, 'actor_system_already_running', actor.actor_system_already_running), \
                    patch.object(metrics.InMemoryMetricsStore, '_add', metrics.InMemoryMetricsStore._add), \
                    patch.object(metrics.InMemoryMetricsStore, 'bulk_add', metrics.InMemoryMetricsStore.bulk_add):
                versions = {'opensearch-benchmark': '2.5.0', 'thespian': '3.10.6', package: bad}
                original = metrics.InMemoryMetricsStore._add
                with patch('importlib.metadata.version', side_effect=versions.__getitem__), \
                        patch('os._exit', side_effect=SystemExit) as exit_process, \
                        self.assertRaises(SystemExit):
                    load_capture_file('sitecustomize')
                exit_process.assert_called_once_with(86)
                self.assertIs(metrics.InMemoryMetricsStore._add, original)
                self.assertEqual(getattr(actor, '__SYSTEM_BASE'), 'multiprocTCPBase')


class QueueEchoActor(Actor):
    def receiveMessage(self, message, sender):
        if isinstance(message, str):
            self.send(sender, [os.getpid(), message])


class RealQueueIsolationTests(unittest.TestCase):
    def test_two_native_queue_lifecycles_coexist_and_leave_tcp_sentinel_untouched(self):
        # Bind without connecting or adopting any existing service. If port 1900
        # is occupied, leave it entirely alone; mocked race tests still run.
        with socket.socket() as sentinel, tempfile.TemporaryDirectory() as temp:
            try:
                sentinel.bind(('127.0.0.1', 1900))
            except OSError as exc:
                self.skipTest('Port 1900 unavailable; existing service untouched: ' + str(exc))
            sentinel.listen()
            sentinel.setblocking(False)
            root = Path(temp)
            code = '''import json, os, time
from pathlib import Path
from unittest.mock import patch
from osbenchmark import actor, benchmark, config
from thespian.actors import ActorSystem
from test_actor_isolation import QueueEchoActor
root = Path(os.environ['OSB_CAPTURE'])
label = root.name
cfg = config.Config()
def run(cfg):
    system = ActorSystem()
    assert type(system._systemBase).__module__ == 'thespian.system.multiprocQueueBase'
    echo = system.createActor(QueueEchoActor, globalName='same-name-in-both-private-systems')
    first = system.ask(echo, label, 5)
    assert first and first[1] == label, first
    (root / 'ready').write_text(json.dumps(first))
    if label == 'first':
        deadline = time.monotonic() + 25
        while not (root / 'release').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('second private system did not finish')
            time.sleep(.05)
    again = system.ask(echo, label + '-after', 5)
    assert again == [first[0], label + '-after'], again
    (root / 'response').write_text(json.dumps(again))
with patch.object(actor.log, 'load_configuration', return_value={}):
    benchmark.with_actor_system(run, cfg)
assert cfg.opts('system', 'remote.benchmarking.supported') is False
assert getattr(ActorSystem, 'systemBase', None) is None
(root / 'done').touch()
'''
            with ExitStack() as stack:
                def launch(label):
                    capture = root / label
                    capture.mkdir()
                    owner = dict(token=uuid.uuid4().hex, capture=str(capture),
                                 python=str(Path(sys.executable).resolve()), started=time.time()-1)
                    env = dict(os.environ, OSB_CAPTURE=str(capture), OSB_PROCESS_OWNER=owner['token'],
                               PYTHONPATH=os.pathsep.join((str(CAPTURE), str(CAPTURE.parent))),
                               THESPLOG_FILE=str(capture / 'thespian.log'))
                    log = stack.enter_context((capture / 'log').open('w'))
                    child = subprocess.Popen([sys.executable, '-c', code], env=env, stdout=log, stderr=log)
                    # Exact Popen identity plus the existing marker+capture+exe
                    # helper; never global shutdown, port-based kill, or pkill.
                    def cleanup():
                        try:
                            if child.poll() is None:
                                child.kill()
                            child.wait(timeout=5)
                        finally:
                            stop_owned_processes(owner)
                    stack.callback(cleanup)
                    return child, capture, owner

                first, first_root, first_owner = launch('first')
                deadline = time.monotonic() + 20
                while not (first_root / 'ready').exists() and first.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((first_root / 'ready').exists(), (first_root / 'log').read_text())
                second, second_root, second_owner = launch('second')
                self.assertEqual(second.wait(timeout=20), 0, (second_root / 'log').read_text())
                self.assertTrue((second_root / 'done').exists())
                self.assertIsNone(first.poll(), 'Second shutdown affected first process')
                (first_root / 'release').touch()
                self.assertEqual(first.wait(timeout=15), 0, (first_root / 'log').read_text())
                self.assertTrue((first_root / 'done').exists())
                first_response = json.loads((first_root / 'response').read_text())
                second_response = json.loads((second_root / 'response').read_text())
                self.assertNotEqual(first_response[0], second_response[0])
                self.assertEqual(first_response[1], 'first-after')
                self.assertEqual(second_response[1], 'second-after')
                self.assertEqual(owned_processes(first_owner), [])
                self.assertEqual(owned_processes(second_owner), [])
                # No startup, fallback, or shutdown status/reuse probe touched it.
                with self.assertRaises(BlockingIOError):
                    connection, _ = sentinel.accept()
                    connection.close()
                self.assertEqual(sentinel.getsockname(), ('127.0.0.1', 1900))


if __name__ == '__main__':
    unittest.main()
