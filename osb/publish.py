#!/usr/bin/env python3
"""Publish only schema-checked synthetic observations, never private tool logs."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil

from analyze import (analyze, inventory, load_json, need, rows, safe_file, sha, verify_hashes,
                     CODE_FILES, BUILD_FILES)


def fields(value, allowed, label):
    need(isinstance(value, dict) and set(value) <= set(allowed.split()), 'Unsafe fields in ' + label)


def matches(value, pattern, label):
    need(isinstance(value, str) and re.fullmatch(pattern, value), 'Unsafe value in ' + label)


def numeric_tree(value, allowed, label):
    if isinstance(value, dict):
        fields(value, allowed, label)
        for child in value.values():
            numeric_tree(child, allowed, label)
    elif isinstance(value, list):
        for child in value:
            numeric_tree(child, allowed, label)
    elif isinstance(value, str):
        matches(value, r'\d+(?:\.\d+)?(?:nanos|micros|ms|s|m|h|d)', label)
    else:
        need(type(value) in (int, float, bool), 'Unsafe nonnumeric ' + label)


# OpenSearch 2.19 filtered index/JVM counters. Unknown metadata is rejected,
# not silently copied just because it occurred inside a nominally safe receipt.
STAT_FIELDS = '''indices nodes _all primaries total docs count deleted store size_in_bytes reserved_in_bytes
indexing index_total index_time_in_millis index_current index_failed delete_total delete_time_in_millis
 delete_current noop_update_total is_throttled throttle_time_in_millis doc_status get total time_in_millis
 getTime exists_total exists_time_in_millis missing_total missing_time_in_millis current search open_contexts
 query_total query_time_in_millis query_current concurrent_query_total concurrent_query_time_in_millis
 concurrent_query_current concurrent_avg_slice_count fetch_total fetch_time_in_millis fetch_current
 scroll_total scroll_time_in_millis scroll_current point_in_time_total point_in_time_time_in_millis
 point_in_time_current suggest_total suggest_time_in_millis suggest_current search_idle_reactivate_count_total
 merges current_docs current_size_in_bytes total_time_in_millis total_docs total_size_in_bytes
 total_stopped_time_in_millis total_throttled_time_in_millis total_auto_throttle_in_bytes
 unreferenced_file_cleanups_performed refresh external_total external_total_time_in_millis listeners flush
 periodic warmer query_cache memory_size_in_bytes total_count hit_count miss_count cache_size cache_count
 evictions fielddata completion segments memory_in_bytes terms_memory_in_bytes stored_fields_memory_in_bytes
 term_vectors_memory_in_bytes norms_memory_in_bytes points_memory_in_bytes doc_values_memory_in_bytes
 index_writer_memory_in_bytes version_map_memory_in_bytes fixed_bit_set_memory_in_bytes max_unsafe_auto_id_timestamp
 remote_store upload total_upload_size started_bytes succeeded_bytes failed_bytes refresh_size_lag total_bytes
 max_bytes max_refresh_time_lag_in_millis total_time_spent_in_millis download total_download_size
 segment_replication max_bytes_behind total_bytes_behind max_replication_lag file_sizes translog operations
 uncommitted_operations uncommitted_size_in_bytes earliest_last_modified_age request_cache recovery
 current_as_source current_as_target throttle_time_in_millis jvm gc collectors young old collection_count
 collection_time_in_millis pressure total_rejections total_uploads started succeeded failed'''


def validate_stats(stats):
    # The collector's public display name contains spaces; normalize only for
    # schema validation, preserving the original bytes in publication.
    def normalize(value):
        if isinstance(value, dict):
            return {('g1_concurrent_gc' if k == 'G1 Concurrent GC' else k): normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value
    numeric_tree(normalize(stats), STAT_FIELDS + ' g1_concurrent_gc', 'engine counters')


def validate_receipt(receipt):
    fields(receipt, '''name variant smoke control quick preflight image start end nodes errors clean osb_version python
        limits config sources documents corpus_sha256 workload_sha256 engine_image_ids engine_version before_stats
        osb_command osb_exitcode after_stats execution_ok cleanup_errors driver_limits effective_engine_limits
        metric_drop_warning_count memory_store_drop_warning_count source_manifest_sha256 start_commit actor_system''', 'receipt')
    need(receipt['errors'] == [] and receipt.get('cleanup_errors', []) == [], 'Private error text in receipt')
    fields(receipt['preflight'], '''logical_cpus physical_cpus available_memory_bytes disk_free_bytes platform
        docker_endpoint system architecture kernel_release os_id os_version_id cpu_model''', 'preflight')
    for key, value in receipt['preflight'].items():
        if isinstance(value, str):
            if key == 'platform':
                need(value == 'Linux x86_64 / NixOS / WSL2', 'Unknown legacy platform text')
            else:
                matches(value, r'[A-Za-z0-9_.()+ @-]{1,160}', 'preflight.' + key)
        else:
            need(type(value) is int and value >= 0, 'Invalid preflight number')
    fields(receipt['engine_version'], '''distribution number build_type build_hash build_date build_snapshot
        lucene_version minimum_wire_compatibility_version minimum_index_compatibility_version''', 'engine version')
    for value in receipt['engine_version'].values():
        need(type(value) is bool or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:+-]{1,64}', value)),
             'Unsafe engine version value')
    for key in ('before_stats', 'after_stats'):
        validate_stats(receipt[key])
    for name, digest in receipt['sources'].items():
        need(name in CODE_FILES or name in {'osb/' + n for n in CODE_FILES | {
            'LifecycleContract.java', 'analyze.py', 'publish.py', 'requirements.in', 'run.sh', 'test_osb.py'}}, 'Unsafe source name')
        matches(digest, r'[0-9a-f]{64}', 'source hash')
    matches(receipt['python'], r'3\.11\.\d+', 'Python version')
    # The live runner replaces the checkout path and owned loopback address.
    # Reject extra arguments or unredacted paths/targets, including in otherwise
    # allowlisted command fields. Private log *contents* are never read/copied.
    literal = {'$REPO/.local/osb/venv311/bin/opensearch-benchmark', 'run', '--pipeline=benchmark-only',
               '--offline', '--quiet', '--enable-assertions', '--target-hosts=$OWNED_LOOPBACK', '--on-error=abort',
               '--client-options=timeout:30,max_retries:0,retry_on_timeout:false', '--results-format=csv'}
    path = r'\$REPO/\.local/osb/(?:smoke|parallel-smoke|campaign)-\d{8}T\d{6}Z/' + re.escape(receipt['name'])
    for arg in receipt['osb_command']:
        need(arg in literal or re.fullmatch(r'--test-run-id=[0-9a-f-]{36}', arg) or
             re.fullmatch(r'--workload-path=' + path + '/workload', arg) or
             re.fullmatch(r'--results-file=' + path + '/private/summary.csv', arg), 'Unsafe OSB command')


UBQ_FIELDS = '''batches created deleted failures noops requests_per_second retries throttled throttled_until
    throttled_millis throttled_until_millis timed_out took total updated version_conflicts'''
EVENT_FIELDS = {
    'correctness': 'documents revision_sum', 'headers': 'case count expected opaque resource_headers response status',
    'scroll-failure': 'headers opaque response status', 'expired-scroll': 'headers opaque response status',
    'default-client': 'aiohttp outcome response server_status exception_type message parser_diagnostic_status',
    'ubq-start': 'documents', 'ubq-end': 'response duration', 'progress': 'request_start updated batches completed',
    'control-start': '', 'control-end': '',
}


def validate_failure(value):
    if isinstance(value, dict):
        fields(value, 'error status caused_by reason type root_cause failed_shards index shard grouped phase', 'scroll failure')
        for child in value.values():
            validate_failure(child)
    elif isinstance(value, list):
        for child in value:
            validate_failure(child)
    elif isinstance(value, str):
        need(value in {'Cannot parse scroll id', 'Last unit does not have enough valid bits',
                       'illegal_argument_exception', 'search_context_missing_exception',
                       'search_phase_execution_exception', 'query', 'all shards failed'} or
             re.fullmatch(r'No search context found for id \[\d+\]', value), 'Unsafe scroll failure text')
    else:
        need(value is None or type(value) in (int, bool), 'Unsafe scroll failure value')


def validate_events(events):
    for event in events:
        kind = event['kind']
        need(kind in EVENT_FIELDS, 'Unsafe event kind')
        fields(event, 'kind time ' + EVENT_FIELDS[kind], 'event ' + kind)
        if 'response' in event:
            response = event['response']
            if kind in ('scroll-failure', 'expired-scroll'):
                validate_failure(response)
            elif kind == 'headers' and event['case'] == 'search':
                fields(response, '_shards hits timed_out took', 'search response')
                fields(response['hits'], 'hits max_score total', 'search hits')
                need(response['hits']['hits'] == [] and response['hits']['max_score'] is None and
                     response['hits']['total'] == {'relation': 'eq', 'value': 128}, 'Unsafe search hits')
                need(type(response['took']) is int and response['took'] >= 0 and
                     type(response['timed_out']) is bool, 'Unsafe search response scalar')
                numeric_tree(response['_shards'], 'failed skipped successful total', 'search shards')
            else:
                fields(response, UBQ_FIELDS, 'UBQ response')
                need(response['failures'] == [], 'Unsafe UBQ failure details')
                numeric_tree(response, UBQ_FIELDS + ' bulk search', 'UBQ response')
        for header in event.get('resource_headers', []):
            resource = json.loads(header)
            fields(resource, 'action taskId parentTaskId nodeId taskResourceUsage', 'resource header')
            need(resource['action'] == 'indices:data/read/search[phase/query+fetch/scroll]', 'Unsafe header action')
            matches(resource['nodeId'], r'[A-Za-z0-9_-]{22}', 'ephemeral node ID')
            need(type(resource['taskId']) is int and type(resource['parentTaskId']) is int, 'Unsafe task ID')
            numeric_tree(resource['taskResourceUsage'], 'cpu_time_in_nanos memory_in_bytes', 'task usage')
        if kind == 'default-client':
            need(event['aiohttp'] == '3.14.5' and event['outcome'] in ('accepted', 'client-parser-rejection'),
                 'Unsafe default-client metadata')
            common = {'kind', 'time', 'aiohttp', 'outcome', 'server_status'}
            extra = {'response'} if event['outcome'] == 'accepted' else {
                'exception_type', 'message', 'parser_diagnostic_status'}
            need(set(event) == common | extra, 'Unsafe outcome-specific default-client fields')
            if event['outcome'] == 'client-parser-rejection':
                need(event['exception_type'] == 'ClientResponseError' and event['message'] == 'Too many headers received' and
                     event['parser_diagnostic_status'] == 400, 'Unsafe client exception')
        # All remaining scalar fields have bounded synthetic identifiers or numbers.
        strings = {'case': {'search', 'batch16', 'batch1', 'repeat1'},
                   'opaque': {'osb-synthetic-search', 'osb-synthetic-batch16', 'osb-synthetic-batch1',
                              'osb-synthetic-repeat1', 'osb-failure', 'osb-expired'}}
        for key, allowed in strings.items():
            if key in event:
                need(event[key] in allowed, 'Unsafe event identifier')
        if 'headers' in event:
            need(event['headers'] == [], 'Unexpected error headers')
        for key in ('time', 'documents', 'revision_sum', 'count', 'expected', 'status', 'server_status',
                    'duration', 'request_start', 'updated', 'batches', 'completed'):
            if key in event:
                need(event[key] is None or type(event[key]) in (int, float, bool), 'Unsafe event scalar')


def validate_metadata(root, report):
    build = load_json(safe_file(root, 'build.json'))
    fields(build, '''source_commit source_blob stock_image stock_image_id patch_sha256 jdk compiler contract
        variants sole_intervention_entries stock_jar_sha256 input_sha256''', 'build manifest')
    need(build['compiler'] == 'javac --release 11 -proc:none' and
         build['contract'] == 'PASS: success, failure, cancellation callback, context capture, unrelated state\n',
         'Unsafe build diagnostics')
    need(build['jdk'] == 'openjdk version "21.0.6" 2025-01-21 LTS\n'
         'OpenJDK Runtime Environment Temurin-21.0.6+7 (build 21.0.6+7-LTS)\n'
         'OpenJDK 64-Bit Server VM Temurin-21.0.6+7 (build 21.0.6+7-LTS, mixed mode, sharing)\n',
         'Unexpected JDK diagnostics')
    for variant in build['variants'].values():
        fields(variant, 'image_id jar_sha256 class_sha256 source_sha256 changed_entries_from_stock', 'build variant')
        for key in ('jar_sha256', 'class_sha256', 'source_sha256'):
            matches(variant[key], r'[0-9a-f]{64}', 'build digest')
    for key in ('patch_sha256', 'stock_jar_sha256'):
        matches(build[key], r'[0-9a-f]{64}', 'build digest')
    matches(build['source_blob'], r'[0-9a-f]{40}', 'source blob')
    if (root/'source-manifest.json').exists():
        source = load_json(safe_file(root, 'source-manifest.json'))
        fields(source, '''start_commit worktree_status worktree_status_scope files build_manifest_sha256 execution
            python started_utc''', 'source manifest')
        need(source['execution'] == 'fresh interpreter executes code/run.py; workload and metrics hook loaded from this snapshot',
             'Unexpected execution description')
        matches(source['python'], r'3\.11\.\d+ \(main, [A-Za-z0-9 ,:]+\) \[Clang [0-9.]+ \]', 'snapshot Python')
        matches(source['started_utc'], r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', 'snapshot date')
        software = load_json(safe_file(root, 'software.json'))
        fields(software, 'distributions osb_source_tag_commit critical_osb_files', 'software inventory')
        for name, version in software['distributions'].items():
            matches(name, r'[A-Za-z0-9_.-]+', 'distribution name')
            matches(version, r'\d[0-9A-Za-z.+!-]*', 'distribution version')
    for name in report['runs']:
        unit = root/name
        validate_receipt(load_json(safe_file(unit, 'receipt.json')))
        validate_events(rows(safe_file(unit, 'raw/events.jsonl')))
        for sample in load_json(safe_file(unit, 'host.json')):
            fields(sample, 'time cpu_percent available_bytes swap_in swap_out unrelated_containers', 'host sample')
            need(all(type(v) in (int, float) for v in sample.values()), 'Unsafe host sample')
        raw_files = inventory(unit/'raw')
        need(all(n == 'events.jsonl' or re.fullmatch(r'metrics-\d+\.jsonl', n) or n == 'metrics.jsonl.gz'
                 for n in raw_files), 'Unexpected raw capture files')
        for path in metric_files(unit):
            for metric in rows(path):
                fields(metric, '@timestamp relative-time-ms name value unit task operation sample-type meta', 'metric')
                fields(metric['meta'], 'success hits hits_relation timed_out error-type took', 'metric metadata')
                for key, value in metric['meta'].items():
                    need((key == 'hits_relation' and value == 'eq') or
                         (key == 'error-type' and value in ('timeout', 'transport')) or
                         (key not in ('hits_relation', 'error-type') and type(value) in (int, float, bool)),
                         'Unsafe metric metadata value')
                for key in ('task', 'operation'):
                    need(metric[key] in {'search', 'create-index', 'bulk', 'refresh', 'force-merge',
                        'initial-correctness', 'final-correctness', 'warmup-search', 'recovery-search',
                        'concurrent-search', 'load', 'header-lifecycle', 'verify-corpus'}, 'Unsafe metric operation')
                need(metric['unit'] in ('ms', 'ops/s', 'docs/s'), 'Unsafe metric unit')


def metric_files(unit):
    return [safe_file(unit, 'raw/' + p.name) for p in sorted((unit/'raw').glob('metrics*.jsonl*'))]


def source_files(source, report):
    allowed = {'order.json', 'build.json', 'PROTOCOL.md'}
    if (source/'source-manifest.json').exists():
        allowed |= {'source-manifest.json', 'software.json'} | {'code/' + name for name in load_json(source/'source-manifest.json')['files']}
        if 'input_sha256' in load_json(source/'build.json'):
            allowed |= {'build-inputs/' + name for name in BUILD_FILES}
    for name in report['runs']:
        allowed |= {name + '/' + f for f in ('receipt.json', 'host.json', 'raw/events.jsonl',
                                            'workload/workload.json', 'workload/index.json')}
    return allowed


def publication_files(source, report):
    return source_files(source, report) | {'SHA256.json', 'report.json'} | {
        name + '/' + f for name in report['runs'] for f in ('raw/metrics.jsonl.gz', 'provenance.json')}


def publish(source, destination):
    source, destination = Path(source), Path(destination)
    report = analyze(source)  # Private source needs no publication checksum manifest.
    validate_metadata(source, report)  # Validate everything before creating an output directory.
    paths = [(name, safe_file(source, name)) for name in sorted(source_files(source, report))]
    destination.mkdir(parents=True, exist_ok=False)
    for name, path in paths:
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    for name in report['runs']:
        unit, out = source/name, destination/name
        files = metric_files(unit)
        raw = b''.join(gzip.decompress(p.read_bytes()) if p.suffix == '.gz' else p.read_bytes() for p in files)
        (out/'raw/metrics.jsonl.gz').write_bytes(gzip.compress(raw, mtime=0))
        provenance = {'projection': 'OSB metric allowlist at creation; exact concatenation, no deduplication or sampling',
                      'raw_concatenation_sha256': hashlib.sha256(raw).hexdigest(),
                      'source_metric_file_sha256': [sha(p) for p in files]}
        (out/'provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')
    (destination/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    hashes = {name: sha(safe_file(destination, name)) for name in sorted(inventory(destination))}
    (destination/'SHA256.json').write_text(json.dumps(hashes, indent=2)+'\n')
    verify_hashes(destination)
    need(analyze(destination) == report, 'Published projection changes recomputed report')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    publish(args.source, args.destination)
