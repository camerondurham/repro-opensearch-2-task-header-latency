"""Bounded, exact-environment ownership checks for OSB's detached actors.

Thespian can setsid()+fork beyond the launcher's process group. A random marker
and per-unit capture path are inherited by those processes, never by other jobs.
No command-name matching, global actor shutdown, or unknown-process killing.
"""
import os
from pathlib import Path
import time
import psutil


def owned_processes(owner):
    found = []
    for process in psutil.process_iter():
        try:
            if process.create_time() < owner['started'] or process.uids().real != os.getuid() or process.status() == psutil.STATUS_ZOMBIE:
                continue
            env = process.environ()
            if env.get('OSB_PROCESS_OWNER') != owner['token']:
                continue
            if env.get('OSB_CAPTURE') != owner['capture'] or str(Path(process.exe()).resolve()) != owner['python']:
                raise RuntimeError('Tagged process identity mismatch; refusing to signal')
            found.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            # A terminating process may be transiently nondumpable before exit.
            # Wait for exit only; never signal a process whose identity is unknown.
            try:
                process.wait(timeout=.2)
            except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                pass
            try:
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    continue
            except psutil.NoSuchProcess:
                continue
            # A live same-user process that cannot be inspected makes absence uncertain.
            raise RuntimeError('Cannot establish owned-process absence') from exc
    return found


def stop_owned_processes(owner, grace=3):
    """Revalidate process creation identity before signalling; bounded rescans.

    psutil Process signal methods guard PID reuse. Rescans catch children forked
    during termination; a live survivor makes cleanup fail, never clean=True.
    """
    for method in ('terminate', 'kill', 'kill'):
        processes = owned_processes(owner)
        if not processes:
            return
        for process in processes:
            try:
                # Match current process object (PID + create_time), not just PID.
                current = next((p for p in owned_processes(owner) if p == process), None)
                if current is not None:
                    getattr(current, method)()
            except psutil.NoSuchProcess:
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not owned_processes(owner):
                return
            time.sleep(.05)
    if owned_processes(owner):
        raise RuntimeError('Owned OSB actors remain alive after bounded cleanup')
