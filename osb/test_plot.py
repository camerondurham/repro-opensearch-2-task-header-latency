"""Actual retained full-duration observations, not mocked timeline samples."""
import json
from pathlib import Path
import subprocess
import unittest
from analyze import rows
from plot import series, verify

ROOT = Path(__file__).resolve().parents[1]


class PlotTests(unittest.TestCase):
    def test_every_retained_search_belongs_to_exactly_one_phase_bin(self):
        root = ROOT/'results/osb/campaign'
        data = series(root)
        self.assertEqual(len(data), 10)
        for name, bins in data.items():
            metrics = [r for r in rows(root/name/'raw/metrics.jsonl.gz')
                       if r['operation'] == 'search' and r['name'] == 'service_time']
            self.assertEqual(sum(b['count'] for b in bins), len(metrics))
            self.assertEqual({b['task'] for b in bins},
                             {'warmup-search', 'concurrent-search', 'recovery-search'})
            self.assertTrue(all(b['count'] > 0 and b['bin_start'] <= b['first'] <= b['last'] < b['bin_start']+5
                                for b in bins))
        verify(root, ROOT/'figures')

    def test_compatibility_entrypoint_rejects_unknown_modes_without_work(self):
        for args in (['invalid'], ['smoke', 'extra']):
            result = subprocess.run(['bash', str(ROOT/'run-reproduction.sh'), *args], capture_output=True)
            self.assertEqual(result.returncode, 2)
        result = subprocess.run(['bash', str(ROOT/'run-reproduction.sh'), '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'OSB', result.stdout)
