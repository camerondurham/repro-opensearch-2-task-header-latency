"""Generate deterministic input and an explicit native OSB schedule."""
import hashlib
import json
from pathlib import Path
import shutil


def generate(path, *, smoke=False, candidate=False, control=False, quick=False):
    path = Path(path)
    path.mkdir(parents=True)
    docs = 128 if smoke else 4800 if quick else 12000
    corpus = ''.join(json.dumps(row, separators=(',', ':')) + '\n' for i in range(docs) for row in (
        {'index': {'_index': 'fixture', '_id': str(i)}},
        {'uid': i, 'revision': 0, 'group': i % 32, 'payload': 'synthetic-' + str(i % 97)})).encode()
    (path / 'corpus.json').write_bytes(corpus)
    index = {'settings': {'number_of_shards': 1 if smoke else 3, 'number_of_replicas': 0,
                          'refresh_interval': '-1'}, 'mappings': {'properties': {
        'uid': {'type': 'long'}, 'revision': {'type': 'long'}, 'group': {'type': 'integer'},
        'payload': {'type': 'keyword'}}}}
    (path / 'index.json').write_text(json.dumps(index))
    query = {'name': 'search', 'operation-type': 'search', 'index': 'fixture', 'cache': False,
             'detailed-results': True, 'body': {'size': 10, 'track_total_hits': True,
                'query': {'match_all': {}}, 'sort': [{'uid': 'asc'}]},
             'assertions': [{'property': 'hits', 'condition': '==', 'value': docs},
                            {'property': 'timed_out', 'condition': '==', 'value': False}]}
    def search(name, seconds):
        return {'name': name, 'operation': query, 'clients': 2, 'time-period': seconds,
                'target-throughput': 50, 'schedule': 'deterministic'}
    schedule = [{'operation': {'operation-type': 'create-index'}},
                {'operation': {'operation-type': 'bulk', 'bulk-size': 1000}, 'clients': 1},
                {'operation': {'operation-type': 'refresh', 'index': 'fixture'}},
                {'operation': {'operation-type': 'force-merge', 'index': 'fixture', 'max-num-segments': 1}},
                {'name': 'initial-correctness', 'operation': {'operation-type': 'verify-corpus', 'docs': docs, 'revision': 0}}]
    warm = search('warmup-search', 1)
    warm['warmup-time-period'] = 2 if smoke or quick else 20
    schedule.append(warm)
    if smoke:
        schedule.append({'operation': {'operation-type': 'header-lifecycle', 'candidate': candidate,
                                       'request-timeout': 180}})
        revision = 512
    else:
        # OSB 2.5.0 applies request-timeout to the WHOLE custom runner, not
        # each HTTP request. Keep the transport at 30s; bound the lifecycle separately.
        duration = 40 if quick else 100
        operation = {'name': 'load', 'operation-type': 'control-window', 'duration': duration,
                     'request-timeout': duration + 10} if control else {
            'name': 'load', 'operation-type': 'verified-ubq', 'docs': docs, 'batch': 10, 'rate': 120,
            'deadline': 240, 'request-timeout': 245}
        schedule.append({'parallel': {'completed-by': 'load', 'tasks': [
            search('concurrent-search', 250), {'name': 'load', 'operation': operation, 'clients': 1, 'iterations': 1}]}})
        revision = 0 if control else docs
    schedule.extend([{'name': 'final-correctness', 'operation': {'operation-type': 'verify-corpus', 'docs': docs, 'revision': revision}},
                     search('recovery-search', 2 if smoke or quick else 10)])
    workload = {'version': 2, 'description': 'Synthetic scroll task-header lifecycle and concurrent search',
        'indices': [{'name': 'fixture', 'body': 'index.json'}],
        'corpora': [{'name': 'synthetic', 'documents': [{'source-file': 'corpus.json', 'document-count': docs,
                     'uncompressed-bytes': len(corpus), 'includes-action-and-meta-data': True, 'target-index': 'fixture'}]}],
        'test_procedures': [{'name': 'controlled', 'default': True, 'schedule': schedule}]}
    (path / 'workload.json').write_text(json.dumps(workload, indent=2) + '\n')
    shutil.copyfile(Path(__file__).with_name('workload.py'), path / 'workload.py')
    return {'documents': docs, 'corpus_sha256': hashlib.sha256(corpus).hexdigest(),
            'workload_sha256': hashlib.sha256((path / 'workload.json').read_bytes()).hexdigest()}
