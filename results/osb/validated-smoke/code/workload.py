"""OSB workload plugin: missing UBQ lifecycle and duplicate-header observation."""
import asyncio
import json
import os
from pathlib import Path
import time
import aiohttp


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def record(kind, **data):
    with (Path(os.environ['OSB_CAPTURE']) / 'events.jsonl').open('a') as out:
        out.write(json.dumps(dict(kind=kind, time=time.time(), **data), sort_keys=True) + '\n')


def check_ubq(body, docs):
    require(body.get('timed_out') is False and body.get('failures') == [], 'UBQ timeout/failures')
    for key, expected in dict(total=docs, updated=docs, deleted=0, noops=0, version_conflicts=0).items():
        require(type(body.get(key)) is int and body[key] == expected, 'UBQ accounting: ' + key)
    require(body.get('retries') == dict(bulk=0, search=0), 'UBQ retries')


async def verify(client, params):
    await client.indices.refresh(index='fixture')
    result = await client.search(index='fixture', body={'size': 0, 'track_total_hits': True,
                                'aggs': {'revision': {'sum': {'field': 'revision'}}}})
    require(result['hits']['total'] == {'value': params['docs'], 'relation': 'eq'}, 'Document count')
    require(result['_shards']['failed'] == 0 and not result['timed_out'], 'Verification search failed')
    require(result['aggregations']['revision']['value'] == params['revision'], 'Revision sum')
    record('correctness', documents=params['docs'], revision_sum=result['aggregations']['revision']['value'])
    return 1, 'ops'


async def ubq(client, params):
    start = time.time()
    docs = params['docs']
    record('ubq-start', documents=docs)
    response = await client.transport.perform_request('POST', '/fixture/_update_by_query',
        params={'wait_for_completion': 'false', 'slices': 1, 'scroll_size': params['batch'],
                'requests_per_second': params['rate'], 'refresh': 'false', 'conflicts': 'abort'},
        body={'query': {'match_all': {}}, 'script': {'source': 'ctx._source.revision += 1'}})
    task = response['task']
    try:
        while time.time() - start < params['deadline']:
            before = time.time()
            response = await client.tasks.get(task_id=task)
            status = response.get('task', {}).get('status', {})
            record('progress', request_start=before, updated=status.get('updated', 0),
                   batches=status.get('batches', 0), completed=response.get('completed', False))
            if response.get('completed'):
                require('error' not in response, 'UBQ task failed')
                result = response['response']
                record('ubq-end', response=result, duration=time.time() - start)
                check_ubq(result, docs)
                return {'weight': docs, 'unit': 'docs', 'success': True}
            await asyncio.sleep(0.5)
        raise TimeoutError('UBQ exceeded declared deadline')
    except BaseException:
        await client.tasks.cancel(task_id=task)
        raise


async def control(client, params):
    record('control-start')
    await asyncio.sleep(params['duration'])
    record('control-end')
    return 1, 'ops'


async def headers(client, params):
    # Dedicated tiny operation: the official client drops duplicate HTTP headers.
    # This is never used to schedule concurrent searches or time the performance arm.
    target = os.environ['OSB_TARGET']
    require(target.startswith('http://127.0.0.1:'), 'Header probe requires owned loopback target')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), max_headers=1024) as session:
        cases = [('search', '/fixture/_search', {'size': 0}, 0)]
        for name, batch in [('batch16', 16), ('batch1', 1), ('repeat1', 1)]:
            expected = 0 if params['candidate'] else 128 // batch
            cases.append((name, f'/fixture/_update_by_query?scroll_size={batch}&slices=1&refresh=true',
                          {'script': {'source': 'ctx._source.revision += 1'}}, expected))
        for name, path, body, expected in cases:
            opaque = 'osb-synthetic-' + name
            async with session.post(target + path, json=body, headers={'X-Opaque-Id': opaque}) as r:
                response = await r.json()
                values = r.headers.getall('TASK_RESOURCE_USAGE', [])
                record('headers', case=name, status=r.status, expected=expected, count=len(values),
                       resource_headers=values, opaque=r.headers.get('X-Opaque-Id'), response=response)
                require(r.status == 200 and r.headers.get('X-Opaque-Id') == opaque, 'HTTP status/opaque header')
                require(len(values) == expected, f'{name}: expected {expected} resource headers, got {len(values)}')
                if name != 'search':
                    check_ubq(response, 128)
        async with session.post(target + '/_search/scroll', json={'scroll_id': 'malformed'},
                                headers={'X-Opaque-Id': 'osb-failure'}) as r:
            response = await r.json()
            record('scroll-failure', status=r.status, response=response,
                   opaque=r.headers.get('X-Opaque-Id'), headers=r.headers.getall('TASK_RESOURCE_USAGE', []))
            require(r.status == 400 and r.headers.get('X-Opaque-Id') == 'osb-failure', 'Malformed scroll contract')
        # Clear a valid scroll, then exercise a real missing-context terminal failure.
        async with session.post(target + '/fixture/_search?scroll=1m', json={'size': 1}) as r:
            scroll = (await r.json())['_scroll_id']
        async with session.delete(target + '/_search/scroll', json={'scroll_id': [scroll]}) as r:
            require(r.status == 200 and (await r.json())['succeeded'], 'Clear scroll')
        async with session.post(target + '/_search/scroll', json={'scroll_id': scroll, 'scroll': '1m'},
                                headers={'X-Opaque-Id': 'osb-expired'}) as r:
            response = await r.json()
            record('expired-scroll', status=r.status, response=response,
                   opaque=r.headers.get('X-Opaque-Id'), headers=r.headers.getall('TASK_RESOURCE_USAGE', []))
            require(r.status == 404 and r.headers.get('X-Opaque-Id') == 'osb-expired', 'Expired scroll contract')
    # Separate compatibility outcome with the pinned client's untouched defaults.
    # ClientResponseError.status is a parser diagnostic, NOT a server HTTP status.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as default_client:
        try:
            async with default_client.post(target + '/fixture/_update_by_query?scroll_size=1&slices=1&refresh=true',
                    json={'script': {'source': 'ctx._source.revision += 1'}}) as response:
                result = await response.json()
                record('default-client', outcome='accepted', server_status=response.status,
                       aiohttp=aiohttp.__version__, response=result)
                require(params['candidate'] and response.status == 200, 'Expected stock default-client rejection')
                check_ubq(result, 128)
        except aiohttp.ClientResponseError as exc:
            record('default-client', outcome='client-parser-rejection', aiohttp=aiohttp.__version__,
                   exception_type=type(exc).__name__, message=exc.message,
                   parser_diagnostic_status=exc.status, server_status=None)
            require(not params['candidate'] and exc.message == 'Too many headers received',
                    'Unexpected default-client failure')
    return 1, 'ops'


def register(registry):
    for name, runner in [('verified-ubq', ubq), ('verify-corpus', verify),
                         ('header-lifecycle', headers), ('control-window', control)]:
        registry.register_runner(name, runner, async_runner=True)
