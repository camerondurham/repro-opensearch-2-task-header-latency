import asyncio
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import importlib.util
import logging
import pickle
import zlib
import unittest
from unittest.mock import AsyncMock, patch

import osbenchmark
from osbenchmark import config
from osbenchmark.workload.loader import WorkloadFileReader
from analyze import paired_interval, p99, verify_hashes
from generate import generate
from workload import check_ubq, ubq
from run import cleanup_owned, LABEL


def good_response():
    return dict(total=128, updated=128, deleted=0, noops=0, version_conflicts=0,
                retries=dict(bulk=0, search=0), timed_out=False, failures=[])


class WorkloadTests(unittest.TestCase):
    def test_real_osb_loader_accepts_all_procedures_and_native_parallel(self):
        cfg = config.Config()
        cfg.add(config.Scope.application, 'node', 'benchmark.root', str(Path(osbenchmark.__file__).parent))
        with tempfile.TemporaryDirectory() as temp:
            for index, options in enumerate((dict(smoke=True), dict(smoke=True, candidate=True), {}, dict(control=True), dict(quick=True))):
                path = Path(temp) / str(index)
                generate(path, **options)
                work = WorkloadFileReader(cfg).read('synthetic', str(path / 'workload.json'), str(path))
                self.assertEqual(1, len(work.test_procedures))
                if not options.get('smoke'):
                    parallel = work.default_test_procedure.schedule[6]
                    self.assertEqual(3, parallel.clients)
                    self.assertEqual(1, sum(t.completes_parent for t in parallel.tasks))

    def test_corpus_identical_across_intervention_and_control(self):
        with tempfile.TemporaryDirectory() as temp:
            a = generate(Path(temp)/'a')
            b = generate(Path(temp)/'b', candidate=True)
            c = generate(Path(temp)/'c', control=True)
            self.assertEqual(a['corpus_sha256'], b['corpus_sha256'])
            self.assertEqual(a['corpus_sha256'], c['corpus_sha256'])
            self.assertEqual(a['workload_sha256'], b['workload_sha256'])
            lines = [json.loads(line) for line in (Path(temp)/'a/corpus.json').read_text().splitlines()]
            self.assertEqual([row['index']['_id'] for row in lines[::2]], [str(i) for i in range(12000)])
            self.assertTrue(all(row['revision'] == 0 for row in lines[1::2]))

    def test_failed_or_partial_updates_rejected(self):
        check_ubq(good_response(), 128)
        for key, bad in [('updated', 127), ('total', 129), ('timed_out', True), ('failures', [{}]),
                         ('version_conflicts', 1), ('retries', {'bulk': 1, 'search': 0}), ('updated', True)]:
            response = good_response()
            response[key] = bad
            with self.assertRaises(RuntimeError, msg=key):
                check_ubq(response, 128)

    def test_ubq_timeout_cancels_only_its_task(self):
        client = AsyncMock()
        client.transport.perform_request.return_value = {'task': 'synthetic:1'}
        with patch('workload.record'):
            with self.assertRaises(TimeoutError):
                asyncio.run(ubq(client, dict(docs=128, batch=1, rate=10, deadline=0)))
        client.tasks.cancel.assert_awaited_once_with(task_id='synthetic:1')

    def test_task_error_is_not_success(self):
        client = AsyncMock()
        client.transport.perform_request.return_value = {'task': 'synthetic:2'}
        client.tasks.get.return_value = {'completed': True, 'error': {'reason': 'failed'}}
        with patch('workload.record'):
            with self.assertRaisesRegex(RuntimeError, 'task failed'):
                asyncio.run(ubq(client, dict(docs=128, batch=1, rate=10, deadline=5)))
        client.tasks.cancel.assert_awaited_once_with(task_id='synthetic:2')

    def test_success_checks_terminal_accounting(self):
        client = AsyncMock()
        client.transport.perform_request.return_value = {'task': 'synthetic:3'}
        client.tasks.get.return_value = {'completed': True, 'response': good_response()}
        with patch('workload.record'):
            result = asyncio.run(ubq(client, dict(docs=128, batch=1, rate=10, deadline=5)))
        self.assertEqual(result['weight'], 128)
        client.tasks.cancel.assert_not_awaited()


class AnalysisTests(unittest.TestCase):
    def test_metrics_capture_does_not_duplicate_native_store_transfers(self):
        from osbenchmark.metrics import MetricsStore
        class Store:
            bulk_add = MetricsStore.bulk_add
            def __init__(self):
                self.docs = []
                self.logger = logging.getLogger('capture-test')
            def _add(self, doc):
                self.docs.append(doc)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, OSB_CAPTURE=temp), \
                patch('osbenchmark.metrics.InMemoryMetricsStore', Store):
            spec = importlib.util.spec_from_file_location('capture_test', Path(__file__).parent/'capture/sitecustomize.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            store = Store()
            doc = {'name': 'service_time', 'value': 2, 'meta': {'success': True, 'private': 'must not publish'}}
            store._add(doc)
            store.bulk_add(zlib.compress(pickle.dumps([doc])))
            self.assertEqual(store.docs, [doc, doc])
            captured = [json.loads(line) for p in Path(temp).glob('metrics-*') for line in p.read_text().splitlines()]
            self.assertEqual(len(captured), 1)
            self.assertNotIn('private', captured[0]['meta'])

    def test_nearest_rank_and_invalid_samples(self):
        self.assertEqual(p99(list(range(100))), 98)
        for bad in ([], [math.nan], [math.inf], [-1]):
            with self.assertRaises(ValueError): p99(bad)

    def test_uncertainty_uses_four_run_effects(self):
        self.assertEqual(paired_interval([0]*4), [1, 1])
        self.assertTrue(paired_interval([-.5, .5, -.5, .5])[0] < 1)
        self.assertTrue(paired_interval([-.5, .5, -.5, .5])[1] > 1)
        with self.assertRaises(ValueError): paired_interval([0]*4000)

    def test_tampered_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'raw.json').write_text('changed')
            (root/'SHA256.json').write_text(json.dumps({'raw.json': '0'*64}))
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'): verify_hashes(root)


class CleanupTests(unittest.TestCase):
    def simulate(self, fail_inspect=False, wrong_owner=False):
        owner = 'osbth-test'
        calls, remaining = [], {'one', 'two'}
        network = [True]
        def command(args):
            args = args[3:]  # docker --context default
            calls.append(args)
            if args[0] == 'ps': return ' '.join(sorted(remaining))
            if args[:2] == ['network', 'ls']: return 'net' if network[0] else ''
            if args[0] == 'inspect':
                if fail_inspect and args[1] == 'one': raise RuntimeError('inspect unavailable')
                return json.dumps([{'Config': {'Labels': {LABEL: 'foreign' if wrong_owner and args[1] == 'one' else owner}},
                                    'Name': '/' + owner + ('-n0' if args[1] == 'one' else '-n1')}])
            if args[0] == 'logs': raise RuntimeError('log collection unavailable')
            if args[:2] == ['rm', '-f']:
                remaining.remove(args[2]); return ''
            if args[:2] == ['network', 'inspect']:
                return json.dumps([{'Labels': {LABEL: owner}, 'Name': owner+'-net', 'Containers': list(remaining)}])
            if args[:2] == ['network', 'rm']:
                network[0] = False; return ''
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as temp, patch('run.command', command):
            errors = cleanup_owned(owner, owner+'-net', ['one', 'two'], Path(temp), None)
        return calls, errors, remaining, network[0]

    def test_log_failure_does_not_prevent_other_cleanup(self):
        calls, errors, remaining, network = self.simulate()
        self.assertFalse(remaining or network)
        self.assertEqual(len(errors), 2)
        self.assertIn(['network', 'rm', 'net'], calls)

    def test_inspection_failure_keeps_unverified_resource_but_cleans_other(self):
        calls, errors, remaining, network = self.simulate(fail_inspect=True)
        self.assertEqual(remaining, {'one'})
        self.assertIn(['rm', '-f', 'two'], calls)
        self.assertNotIn(['rm', '-f', 'one'], calls)
        self.assertTrue(network and errors)

    def test_owner_mismatch_never_removes_foreign_resource(self):
        calls, errors, remaining, network = self.simulate(wrong_owner=True)
        self.assertEqual(remaining, {'one'})
        self.assertNotIn(['rm', '-f', 'one'], calls)
        self.assertTrue(network and errors)


if __name__ == '__main__':
    unittest.main()
