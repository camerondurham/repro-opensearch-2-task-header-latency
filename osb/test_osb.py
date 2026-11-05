import asyncio
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import osbenchmark
from osbenchmark import config
from osbenchmark.workload.loader import WorkloadFileReader
from analyze import paired_interval, p99, verify_hashes
from generate import generate
from workload import check_ubq, ubq


def good_response():
    return dict(total=128, updated=128, deleted=0, noops=0, version_conflicts=0,
                retries=dict(bulk=0, search=0), timed_out=False, failures=[])


class WorkloadTests(unittest.TestCase):
    def test_real_osb_loader_accepts_all_procedures_and_native_parallel(self):
        cfg = config.Config()
        cfg.add(config.Scope.application, 'node', 'benchmark.root', str(Path(osbenchmark.__file__).parent))
        with tempfile.TemporaryDirectory() as temp:
            for index, options in enumerate((dict(smoke=True), dict(smoke=True, candidate=True), {}, dict(control=True))):
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


if __name__ == '__main__':
    unittest.main()
