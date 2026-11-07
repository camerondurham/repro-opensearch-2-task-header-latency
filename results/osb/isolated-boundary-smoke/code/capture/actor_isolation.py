"""Process-private actor ownership for pinned OSB capture runs.

Thespian 3.10.6 documents in thespian/system/multiprocQueueBase.py that
queues are unique to the creating process family: sibling processes cannot
share the system, and no inter-system communication is possible. Select that
native transport through OSB\'s official offline API, not a TCP port check:
a foreign TCP admin can appear between checking a port and bootstrap.

OSB 2.5.0 still probes TCP:1900 before bootstrap and after shutdown, even in
queue mode. Bypass only that irrelevant probe, guarded by the pinned actor
base. Native benchmark.with_actor_system and bootstrap_actor_system retain
all creation, execution, timing, and finally/shutdown behavior. There are no
scheduler, query, or timing patches. Queue mode cannot run remote workers.

Importing this module is inert. sitecustomize calls install only when
OSB_CAPTURE is set, before importing benchmark. Tests that install/import
sitecustomize must restore actor.__SYSTEM_BASE and the probe callback.
"""
from importlib.metadata import version


ACTOR_SYSTEM = 'multiprocQueueBase'


def queue_system_already_running(ip="127.0.0.1"):
    """Queue systems have no shared TCP administrator to discover or reuse."""
    from osbenchmark import actor
    # Private-name read is intentional and version-pinned. Never silently
    # suppress the safety probe if a later change selects a network base.
    if getattr(actor, '__SYSTEM_BASE', None) != ACTOR_SYSTEM:
        raise RuntimeError('Actor isolation requires multiprocQueueBase; refusing shared actor-system reuse')
    return False


def install():
    """Select native private queues and suppress only the shared-TCP probe."""
    for package, expected in (('opensearch-benchmark', '2.5.0'), ('thespian', '3.10.6')):
        if version(package) != expected:
            raise RuntimeError(f'Actor isolation requires {package} {expected}')
    from osbenchmark import actor
    actor.use_offline_actor_system()
    queue_system_already_running()  # Validate selection before replacing anything.
    actor.actor_system_already_running = queue_system_already_running
