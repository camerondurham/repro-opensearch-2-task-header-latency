"""Offline adversarial checks. All mutations are confined to temporary copies."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from analyze import (analyze, analyze_unit, check_ubq, load_json, sha, verify_hashes, validate_design,
                     BUILD_FILES, CODE_FILES, CAMPAIGN_ORDER, SMOKE_ORDER, LIMITS, locked_distributions)
from generate import generate
from publish import publish
from verify import verify_directory, verify_excluded, verify_default_client_failure

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT/'results/osb'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def edit_json(path, mutate):
    value = load_json(path)
    mutate(value)
    save(path, value)


def edit_rows(path, mutate):
    raw = gzip.decompress(path.read_bytes()).decode() if path.suffix == '.gz' else path.read_text()
    values = [json.loads(line) for line in raw.splitlines()]
    result = mutate(values)
    raw = ''.join(json.dumps(row)+'\n' for row in (values if result is None else result)).encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.suffix == '.gz' else raw)


def checksums(root):
    save(root/'SHA256.json', {p.relative_to(root).as_posix(): sha(p)
                             for p in root.rglob('*') if p.is_file() and p.name != 'SHA256.json'})


def frozen_fixture(root, campaign=False):
    """Synthetic manifests for validator tests, not measurements or build evidence.

    Smoke observations are copied from retained evidence. Campaign unit analysis
    is explicitly stubbed in design tests; no synthetic performance is published.
    """
    root.mkdir()
    build = load_json(EVIDENCE/'smoke/build.json')
    build['input_sha256'] = {}
    for name in sorted(BUILD_FILES):
        path = root/'build-inputs'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('offline fixture only: ' + name)
        build['input_sha256'][name] = sha(path)
    build['patch_sha256'] = build['input_sha256']['proposed-fix.patch']
    for variant in ('baseline', 'candidate'):
        name = next(n for n in BUILD_FILES if n.startswith(variant+'/'))
        build['variants'][variant]['source_sha256'] = build['input_sha256'][name]
    save(root/'build.json', build)
    hashes = {}
    for name in sorted(CODE_FILES):
        path = root/'code'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('offline fixture only: ' + name)
        if name == 'proposed-fix.patch':
            shutil.copyfile(root/'build-inputs'/name, path)
        if name == 'requirements.lock':
            shutil.copyfile(ROOT/'osb/requirements.lock', path)
        hashes[name] = sha(path)
    shutil.copyfile(root/'code/PROTOCOL.md', root/'PROTOCOL.md')
    source = dict(start_commit='a'*40, worktree_status=[], files=hashes,
                  worktree_status_scope='executed snapshot inputs only; independent offline analysis/docs may change',
                  build_manifest_sha256=sha(root/'build.json'),
                  execution='fresh interpreter executes code/run.py; workload and metrics hook loaded from this snapshot',
                  python='3.11.16 (main, Aug 14 2026, 15:35:36) [Clang 22.1.3 ]', started_utc='2026-11-07T06:23:11Z')
    save(root/'source-manifest.json', source)
    software = dict(distributions=locked_distributions(root/'code/requirements.lock'),
        osb_source_tag_commit='445eb5a3188c1e3d3a3ca6bfe87757902ce5b10e', critical_osb_files={
            'metrics.py': '2da2fca8da9741cff28d1e515581c9e4ee8a39182d80e53cc40bfe2164917858',
            'worker_coordinator/worker_coordinator.py': 'e4fdc2cbb2fced5369d21b3991eb14fd29d36361d6e1dc98ca40386e00625ca0',
            'worker_coordinator/runner.py': 'bdeea87e69fb1b648abb4285b06056bd9393ca74f8c364557ccadbec5af4b278',
            'workload/loader.py': '84ed9b3ffe3a9b226eb51dc18b7716c971c44d26f0ae2e88495fde22b29ea657',
            'resources/workload-schema.json': '8cb5c1991dcce3aaea5677342c0f6096b986c15f1038e8bfa1d415329c27eb86'})
    save(root/'software.json', software)
    order = CAMPAIGN_ORDER if campaign else SMOKE_ORDER
    save(root/'order.json', order)
    for index, (name, variant, smoke, control) in enumerate(order):
        unit = root/name
        shutil.copytree(EVIDENCE/'smoke'/variant, unit)
        for path in (unit/'workload').iterdir():
            path.unlink()
        # Generate just fixture files; no OSB, Docker, builds or workloads run.
        (unit/'workload').rmdir()
        generated = generate(unit/'workload', smoke=smoke, candidate=variant == 'candidate', control=control)
        (unit/'workload/workload.py').unlink()  # published projections do not retain a duplicate plugin
        receipt = load_json(unit/'receipt.json')
        nodes = 1 if smoke else 3
        receipt.update(generated, name=name, variant=variant, smoke=smoke, control=control, quick=False, nodes=nodes,
            actor_system='multiprocQueueBase',
            config=dict(batch=10, ubq_rate=120, search_rate=50, search_clients=2,
                        warmup=2 if smoke else 20, deadline=240, recovery=2 if smoke else 10), limits=LIMITS,
            metric_drop_warning_count=0, memory_store_drop_warning_count=0, cleanup_errors=[],
            sources=hashes, start_commit=source['start_commit'], source_manifest_sha256=sha(root/'source-manifest.json'),
            driver_limits=dict(cpus=[6, 7], virtual_address_bytes_per_process=8*2**30),
            effective_engine_limits=[dict(NanoCpus=2*10**9, Memory=3*2**30, MemorySwap=3*2**30,
                                          CpusetCpus=f'{i*2},{i*2+1}') for i in range(nodes)])
        receipt['engine_image_ids'] *= nodes
        if campaign:
            receipt.update(start=1000+index*1000, end=1500+index*1000)
        save(unit/'receipt.json', receipt)


def stub_unit(unit, **_):
    receipt = load_json(unit/'receipt.json')
    return dict(smoke=False, control=receipt['control'], gates=[], ubq_rate=120,
                late_service_p99_ms=7 if receipt['variant'] == 'candidate' else 10)


class TemporaryEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def copied(self, name):
        destination = self.root/name
        shutil.copytree(EVIDENCE/name, destination)
        return destination


class PublishedHashTests(TemporaryEvidence):
    def test_manifest_required_and_exact_inventory(self):
        root = self.copied('smoke')
        verify_hashes(root)
        original = (root/'SHA256.json').read_bytes()
        (root/'SHA256.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing evidence'): verify_hashes(root)
        (root/'SHA256.json').write_bytes(original)
        (root/'private.txt').write_text('must not be ignored')
        with self.assertRaisesRegex(ValueError, 'inventory'): verify_hashes(root)
        (root/'private.txt').unlink()
        edit_json(root/'SHA256.json', lambda v: v.pop('build.json'))
        with self.assertRaisesRegex(ValueError, 'inventory'): verify_hashes(root)

    def test_traversal_absolute_symlinks_and_duplicate_keys(self):
        root = self.root/'bundle'; root.mkdir()
        outside = self.root/'secret'; outside.write_text('outside')
        for name in ('../secret', str(outside), './file', 'a/../file', 'a\\b'):
            with self.subTest(name=name):
                save(root/'SHA256.json', {name: sha(outside)})
                with self.assertRaisesRegex(ValueError, 'Unsafe'): verify_hashes(root)
        (root/'file').symlink_to(outside)
        save(root/'SHA256.json', {'file': sha(outside)})
        with self.assertRaisesRegex(ValueError, 'Symlink'): verify_hashes(root)
        (root/'file').unlink()
        (root/'dir').symlink_to(self.root, target_is_directory=True)
        save(root/'SHA256.json', {'dir/secret': sha(outside)})
        with self.assertRaisesRegex(ValueError, 'Symlink'): verify_hashes(root)
        (root/'SHA256.json').write_text('{"file":"a", "file":"b"}')
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'): verify_hashes(root)

    def test_provenance_validates_decompressed_bytes_even_with_new_outer_hashes(self):
        root = self.copied('parallel-smoke')
        path = root/'parallel-baseline/raw/metrics.jsonl.gz'
        path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes())+b'\n', mtime=0))
        checksums(root)
        with self.assertRaisesRegex(ValueError, 'Decompressed metric provenance'): verify_hashes(root)

    def test_private_analysis_requires_no_checksum_manifest(self):
        root = self.copied('smoke')
        (root/'SHA256.json').unlink()
        self.assertEqual(analyze(root)['evidence_format'], 'legacy-smoke')

    def test_retained_legacy_and_excluded_attempts(self):
        for name in ('smoke', 'parallel-smoke'):
            report = verify_directory(EVIDENCE/name, legacy=True)
            self.assertIn('no frozen executable', report['provenance_limitation'])
            self.assertNotIn('performance_gates_pass', report)
        verify_excluded(EVIDENCE/'excluded-timeout')
        verify_default_client_failure(EVIDENCE/'default-client-original-failure')

    def test_excluded_and_original_failure_must_not_be_reclassified_success(self):
        with patch('verify.analyze_unit', return_value={}):
            with self.assertRaisesRegex(ValueError, 'incorrectly accepted'):
                verify_excluded(EVIDENCE/'excluded-timeout')
        root = self.copied('default-client-original-failure')
        edit_json(root/'failure.json', lambda v: v.update(benchmark_success=True))
        checksums(root)
        with self.assertRaisesRegex(ValueError, 'must remain a failure'): verify_default_client_failure(root)

    def test_fake_exclusion_cannot_bypass_decompressed_provenance(self):
        root = self.copied('smoke')
        (root/'stock/provenance.json').unlink()
        save(root/'exclusion.json', {'excluded': True})
        checksums(root)
        with self.assertRaisesRegex(ValueError, 'excluded-timeout inventory'): verify_hashes(root)

    def test_published_inventory_rejects_hash_listed_extras_and_raw_metric_substitution(self):
        root = self.copied('smoke')
        (root/'extra.txt').write_text('unexpected')
        checksums(root)
        with self.assertRaisesRegex(ValueError, 'published evidence inventory'): verify_directory(root, legacy=True)
        (root/'extra.txt').unlink()
        path = root/'stock/raw/metrics.jsonl.gz'
        (path.parent/'metrics-123.jsonl').write_bytes(gzip.decompress(path.read_bytes()))
        path.unlink(); (root/'stock/provenance.json').unlink()
        checksums(root)
        with self.assertRaisesRegex(ValueError, 'published evidence inventory'): verify_directory(root, legacy=True)


class DesignTests(TemporaryEvidence):
    def setUp(self):
        super().setUp()
        self.campaign = self.root/'campaign'
        frozen_fixture(self.campaign, campaign=True)

    def test_exact_design_and_unchanged_four_pair_endpoints(self):
        with patch('analyze.analyze_unit', side_effect=stub_unit):
            report = analyze(self.campaign)
        self.assertEqual(len(report['pairs']), 4)
        self.assertAlmostEqual(report['geometric_mean_ratio'], .7)
        self.assertEqual(report['mean_difference_ms'], 3)
        self.assertTrue(report['practically_meaningful_effect_supported'])
        self.assertIn('whole-triplet loss cannot be categorically ruled out', report['capture_limitation'])

    def test_audit_regression_no_controls_all_ab_even_with_successful_unit_stubs(self):
        orders = [[], CAMPAIGN_ORDER[1:-1], CAMPAIGN_ORDER + [CAMPAIGN_ORDER[1]],
                  [CAMPAIGN_ORDER[0]] + [[f'pair{p}-{v}', v, False, False] for p in range(1, 5)
                  for v in ('baseline', 'candidate')] + [CAMPAIGN_ORDER[-1]], list(reversed(CAMPAIGN_ORDER)),
                  [['../escape', 'baseline', False, True]]]
        for order in orders:
            with self.subTest(order=order):
                save(self.campaign/'order.json', order)
                with patch('analyze.analyze_unit', side_effect=stub_unit) as units:
                    with self.assertRaises(ValueError): analyze(self.campaign)
                    units.assert_not_called()

    def test_practical_thresholds_and_control_gates_not_lowered(self):
        for baseline, candidate, supported in ((10, 8, True), (10, 8.1, False), (5, 3.5, False), (30, 27, False)):
            def unit(path, **_):
                result = stub_unit(path)
                receipt = load_json(path/'receipt.json')
                result['late_service_p99_ms'] = candidate if receipt['variant'] == 'candidate' else baseline
                return result
            with self.subTest(baseline=baseline, candidate=candidate), patch('analyze.analyze_unit', side_effect=unit):
                self.assertEqual(analyze(self.campaign)['practically_meaningful_effect_supported'], supported)
        def bad_control(path, **_):
            return {**stub_unit(path), 'gates': ['host swapping'] if path.name == 'control-after' else []}
        with patch('analyze.analyze_unit', side_effect=bad_control):
            self.assertFalse(analyze(self.campaign)['performance_gates_pass'])

    def test_receipt_design_mutations(self):
        path = self.campaign/'pair1-baseline/receipt.json'
        original = load_json(path)
        changes = [dict(variant='candidate'), dict(smoke=True), dict(control=True), dict(quick=True),
                   dict(documents=128), dict(nodes=1), dict(corpus_sha256='0'*64),
                   dict(engine_image_ids=['sha256:'+'0'*64]*3), dict(image='mutable:tag'),
                   dict(start_commit='b'*40), dict(source_manifest_sha256='0'*64), dict(sources={}),
                   dict(config={**original['config'], 'search_rate': 1}), dict(limits={**LIMITS, 'node_cpus': 1}),
                   dict(effective_engine_limits=[]), dict(start=0), dict(end=0)]
        for change in changes:
            with self.subTest(change=change):
                save(path, {**original, **change})
                with self.assertRaises(ValueError): validate_design(self.campaign)
        save(path, original)

    def test_workload_index_and_corpus_hashes_are_pinned_not_self_attested(self):
        unit = self.campaign/'pair1-baseline'
        path = unit/'workload/workload.json'; path.write_text('{}')
        edit_json(unit/'receipt.json', lambda r: r.update(workload_sha256=sha(path)))
        with self.assertRaisesRegex(ValueError, 'workload hash'): validate_design(self.campaign)
        # Independent mutations against otherwise valid fixtures.
        for field, file in [('index', 'index.json'), ('corpus', 'corpus.json')]:
            destination = self.root/field; frozen_fixture(destination, campaign=True)
            (destination/'pair1-baseline/workload'/file).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'index hash|Corpus bytes'): validate_design(destination)

    def test_required_frozen_inventory_and_binding(self):
        for name in ('source-manifest.json', 'software.json', 'code/run.py', 'build-inputs/compile.sh'):
            path = self.campaign/name
            contents = path.read_bytes(); path.unlink()
            with self.subTest(name=name), self.assertRaises(ValueError): validate_design(self.campaign)
            path.write_bytes(contents)
        for directory in ('code', 'build-inputs'):
            path = self.campaign/directory/'private.txt'; path.write_text('not public')
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, 'inventory'):
                validate_design(self.campaign)
            path.unlink()
        path = self.campaign/'code/run.py'; path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Frozen file mismatch'): validate_design(self.campaign)

    def test_clean_scoped_start_and_software_required(self):
        path = self.campaign/'source-manifest.json'; original = load_json(path)
        for change in (dict(worktree_status=[' M osb/run.py']), dict(worktree_status_scope='all files'),
                       dict(files={**original['files'], '../private': '0'*64})):
            save(path, {**original, **change})
            with self.subTest(change=change), self.assertRaises(ValueError): validate_design(self.campaign)
        save(path, original)
        edit_json(self.campaign/'software.json', lambda v: v['distributions'].update({'opensearch-benchmark': '2.6.0'}))
        with self.assertRaisesRegex(ValueError, 'software inventory'): validate_design(self.campaign)

    def test_campaign_never_gets_prebuild_boundary_exemption(self):
        build = load_json(self.campaign/'build.json'); del build['input_sha256']
        save(self.campaign/'build.json', build)
        edit_json(self.campaign/'source-manifest.json', lambda v: v.update(build_manifest_sha256=sha(self.campaign/'build.json')))
        with self.assertRaisesRegex(ValueError, 'build-input inventory'): validate_design(self.campaign)


class ObservationTests(TemporaryEvidence):
    def test_audit_regression_every_smoke_ubq_response_checked_including_default_client(self):
        root = self.copied('smoke'); unit = root/'candidate'; path = unit/'raw/events.jsonl'
        original = path.read_bytes()
        for case in ('batch16', 'batch1', 'repeat1', 'default-client'):
            for key, bad in [('updated', 127), ('total', 129), ('deleted', 1), ('noops', 1),
                             ('version_conflicts', 1), ('retries', {'bulk': 1, 'search': 0}),
                             ('timed_out', True), ('failures', [{}]), ('updated', True),
                             ('retries', {'bulk': False, 'search': 0})]:
                path.write_bytes(original)
                def mutate(events):
                    event = next(e for e in events if e.get('case') == case or e['kind'] == case)
                    event['response'][key] = bad
                edit_rows(path, mutate)
                with self.subTest(case=case, key=key), self.assertRaisesRegex(ValueError, 'UBQ'):
                    analyze_unit(unit, legacy=True)

    def test_missing_and_partial_phases_and_duplicate_requests_rejected(self):
        root = self.copied('parallel-smoke'); unit = root/'parallel-baseline'
        path = unit/'raw/metrics.jsonl.gz'; original = path.read_bytes()
        for task, sample in [('warmup-search', 'warmup'), ('warmup-search', 'normal'),
                             ('recovery-search', 'normal'), ('concurrent-search', 'normal'), ('load', 'normal')]:
            path.write_bytes(original)
            edit_rows(path, lambda rs: [r for r in rs if (r['task'], r['sample-type']) != (task, sample)])
            with self.subTest(task=task, sample=sample), self.assertRaisesRegex(ValueError, 'phase coverage'):
                analyze_unit(unit, legacy=True)
        for task in ('warmup-search', 'recovery-search', 'load', 'bulk', 'concurrent-search'):
            path.write_bytes(original)
            def partial(rs):
                rs.remove(next(r for r in rs if r['task'] == task and r['name'] == 'processing_time'))
            edit_rows(path, partial)
            with self.subTest(task=task), self.assertRaisesRegex(ValueError, 'counterpart'):
                analyze_unit(unit, legacy=True)
        path.write_bytes(original)
        edit_rows(path, lambda rs: rs + [next(r for r in rs if r['name'] == 'latency')])
        with self.assertRaisesRegex(ValueError, 'Duplicate metric'): analyze_unit(unit, legacy=True)

    def test_drop_warnings_and_executor_timeout_cannot_be_hidden_by_success_receipt(self):
        root = self.copied('parallel-smoke'); unit = root/'parallel-baseline'
        path = unit/'receipt.json'; original = load_json(path)
        for field in ('metric_drop_warning_count', 'memory_store_drop_warning_count'):
            for missing in (False, True):
                value = copy.deepcopy(original)
                if missing: value.pop(field)
                else: value[field] = 1
                save(path, value)
                with self.subTest(field=field, missing=missing), self.assertRaisesRegex(ValueError, 'drop warning'):
                    analyze_unit(unit, legacy=True)
        save(path, original)
        def timeout(rs):
            for r in rs:
                if r['task'] == 'load' and r['name'] != 'throughput':
                    r['meta']['error-type'] = 'timeout'
        edit_rows(unit/'raw/metrics.jsonl.gz', timeout)
        with self.assertRaisesRegex(ValueError, 'request errors'): analyze_unit(unit, legacy=True)

    def test_host_times_not_just_sample_count(self):
        root = self.copied('parallel-smoke'); unit = root/'parallel-baseline'
        path = unit/'host.json'; original = load_json(path)
        shifted = [{**s, 'time': s['time']+10000} for s in original]
        gap = copy.deepcopy(original); gap[len(gap)//2]['time'] += 5
        for value in (shifted, gap, original[:2], original[-2:]):
            save(path, value)
            with self.assertRaisesRegex(ValueError, 'Host timestamps'): analyze_unit(unit, legacy=True)

    def test_whole_triplet_loss_exceeding_offered_load_tolerance_is_rejected(self):
        root = self.copied('parallel-smoke'); unit = root/'parallel-baseline'
        def drop(rs):
            keys = sorted({(r['@timestamp'], r['relative-time-ms']) for r in rs
                           if r['task'] == 'concurrent-search' and r['name'] == 'service_time'})
            # The retained 128-document run is only 1.5 seconds; its mandated
            # one-second boundary tolerance can legitimately hide a 50% loss.
            lost = set(keys) - set(keys[::10])
            return [r for r in rs if r['task'] != 'concurrent-search' or
                    (r['@timestamp'], r['relative-time-ms']) not in lost]
        edit_rows(unit/'raw/metrics.jsonl.gz', drop)
        with self.assertRaisesRegex(ValueError, 'offered/achieved'): analyze_unit(unit, legacy=True)


class PublicationTests(TemporaryEvidence):
    def setUp(self):
        super().setUp()
        self.source = self.root/'source'
        frozen_fixture(self.source)

    def test_allowlisted_projection_roundtrip_no_private_logs_or_bytecode(self):
        private = self.source/'stock/private'; private.mkdir(); (private/'osb.log').write_text('private marker')
        cache = self.source/'code/__pycache__'; cache.mkdir(); (cache/'build.cpython-311.pyc').write_text('cache')
        destination = self.root/'published'
        publish(self.source, destination)
        verify_directory(destination)
        self.assertEqual(analyze(self.source), analyze(destination))
        with self.assertRaisesRegex(ValueError, 'declared role'):
            verify_directory(destination, expected_format='campaign')
        self.assertFalse(any('private' in str(p.relative_to(destination)) or '__pycache__' in p.parts
                             for p in destination.rglob('*')))
        self.assertEqual(set(p.relative_to(destination/'build-inputs').as_posix()
                             for p in (destination/'build-inputs').rglob('*') if p.is_file()), BUILD_FILES)

    def test_reject_snapshot_extras_even_if_manifest_claims_them(self):
        (self.source/'code/secret.txt').write_text('not public')
        with self.assertRaisesRegex(ValueError, 'inventory'): publish(self.source, self.root/'out')
        edit_json(self.source/'source-manifest.json', lambda v: v['files'].update({'secret.txt': sha(self.source/'code/secret.txt')}))
        with self.assertRaisesRegex(ValueError, 'inventory'): publish(self.source, self.root/'out')
        self.assertFalse((self.root/'out').exists())

    def test_reject_symlink_snapshot_and_receipt(self):
        for relative in ('code/run.py', 'stock/receipt.json'):
            path = self.source/relative; data = path.read_bytes()
            target = self.root/'outside'; target.write_bytes(data); path.unlink(); path.symlink_to(target)
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, 'Symlink'):
                publish(self.source, self.root/'out')
            path.unlink(); path.write_bytes(data)

    def test_reject_private_receipt_fields_nested_fields_and_unredacted_commands(self):
        path = self.source/'stock/receipt.json'; original = load_json(path)
        mutations = [lambda v: v.update(token='private'), lambda v: v['preflight'].update(hostname='private'),
                     lambda v: v['after_stats']['indices'].update(password='secret'),
                     lambda v: v['osb_command'].append('--target-hosts=private.example'),
                     lambda v: v['osb_command'].append('/home/private/osb')]
        for mutation in mutations:
            save(path, original); edit_json(path, mutation)
            with self.assertRaisesRegex(ValueError, 'Unsafe'): publish(self.source, self.root/'out')
            self.assertFalse((self.root/'out').exists())

    def test_reject_private_values_inside_allowed_event_fields(self):
        for unit, mutation in [('stock', lambda es: es[1]['response'].update(took='private')),
                ('stock', lambda es: es[1]['response'].update(timed_out={'private': 'payload'})),
                *[('candidate', lambda es, key=key: next(e for e in es if e['kind'] == 'default-client').update({key: 'private'}))
                  for key in ('message', 'exception_type', 'parser_diagnostic_status')]]:
            path = self.source/unit/'raw/events.jsonl'
            original = path.read_bytes()
            edit_rows(path, mutation)
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                publish(self.source, self.root/'out')
            self.assertFalse((self.root/'out').exists())
            path.write_bytes(original)

    def test_reject_private_event_and_metric_fields(self):
        path = self.source/'stock/raw/events.jsonl'; original = path.read_bytes()
        mutations = [lambda es: es[0].update(authorization='secret'),
                     lambda es: es[2]['response'].update(request_headers={'Authorization': 'secret'}),
                     lambda es: next(e for e in es if e['kind'] == 'expired-scroll')['response']['error'].update(host='secret')]
        for mutation in mutations:
            path.write_bytes(original); edit_rows(path, mutation)
            with self.assertRaisesRegex(ValueError, 'Unsafe fields'): publish(self.source, self.root/'out')
            self.assertFalse((self.root/'out').exists())
        path.write_bytes(original)
        def private_metadata(rs):
            for row in rs:
                row['meta']['password'] = 'secret'
        edit_rows(self.source/'stock/raw/metrics.jsonl.gz', private_metadata)
        with self.assertRaisesRegex(ValueError, 'Unsafe fields'): publish(self.source, self.root/'out')


if __name__ == '__main__':
    unittest.main()
