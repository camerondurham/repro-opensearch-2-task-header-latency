"""Pinned OSB 2.5.0 adapter: retain per-request metrics absent from CSV summary.

No scheduling changes. The normal store still receives the unmodified record.
Capture errors fail the OSB process instead of silently losing observations.
"""
import json
import os
from pathlib import Path

if os.environ.get('OSB_CAPTURE'):
    try:
        from importlib.metadata import version
        if version('opensearch-benchmark') != '2.5.0':
            raise RuntimeError('Metrics adapter requires OSB 2.5.0')
        from osbenchmark.metrics import InMemoryMetricsStore
        original = InMemoryMetricsStore._add
        original_bulk = InMemoryMetricsStore.bulk_add
        root = Path(os.environ['OSB_CAPTURE'])
        def bulk_add(self, memento):
            # OSB transfers the same metrics from worker coordinator to orchestrator.
            # Capture at creation only, not again during that native transfer.
            self._capture_importing = True
            try:
                return original_bulk(self, memento)
            finally:
                self._capture_importing = False
        def capture(self, doc):
            try:
                if not getattr(self, '_capture_importing', False) and doc.get('name') in ('latency', 'service_time', 'processing_time', 'throughput'):
                    safe = {k: doc[k] for k in ('@timestamp', 'relative-time-ms', 'name', 'value', 'unit',
                                               'task', 'operation', 'sample-type') if k in doc}
                    safe['meta'] = {k: v for k, v in doc.get('meta', {}).items() if k in
                                    ('success', 'hits', 'hits_relation', 'timed_out', 'error-type', 'took')}
                    with (root / ('metrics-' + str(os.getpid()) + '.jsonl')).open('a') as out:
                        out.write(json.dumps(safe, sort_keys=True) + '\n')
            except BaseException:
                (root / 'capture-error').touch()
                os._exit(86)
            return original(self, doc)
        InMemoryMetricsStore._add = capture
        InMemoryMetricsStore.bulk_add = bulk_add
    except BaseException:
        os._exit(86)
