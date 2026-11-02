#!/usr/bin/env python3
"""Standalone stock OpenSearch reproducer; Python 3.10+, Linux, Docker and curl."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

IMAGE = "opensearchproject/opensearch@sha256:1f8b88245a6af61e7aa500afe0e87d43401e4b33140bb47230a919428ce3f7cb"
LABEL = "com.opensearch.regression.reproducer"
DOCKER = ("docker", "--context", "default")  # Pin cleanup even if the selected context changes.
LOCK = Path("/tmp/opensearch-regression-lab.lock")
DATA = "/usr/share/opensearch/data"
WORK, READY, CLEANUP = 480, 180, 120
DOCS, HEAP = 128, 536870912

class VerificationError(Exception):
    pass


class Interrupted(VerificationError):
    pass


def need(ok, message):
    if not ok:
        raise VerificationError(message)


def integers(obj, **expected):
    need(all(type(obj.get(k)) is int and obj[k] == v for k, v in expected.items()), f"integer accounting: expected {expected}, got {obj}")


def encode(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def read_json(path):
    value = json.loads(path.read_text())
    need(type(value) is dict, f"{path.name}: expected JSON object")
    return value


def save(path, obj):
    path.write_text(encode(obj) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def steps():
    """One request/expectation table, used both to execute and verify."""
    search = "/fixture/_search?request_cache=false"
    account = {"size": 0, "aggs": {"revision": {"sum": {"field": "revision"}}}}
    bulk = "".join(encode(obj) + "\n" for i in range(DOCS) for obj in (
        {"index": {"_index": "fixture", "_id": str(i)}}, {"uid": str(i), "revision": 0, "payload": "x"}))
    table = [
        ("health", "GET", "/_cluster/health?wait_for_status=green&wait_for_nodes=1&timeout=2s", None, "health", None),
        ("nodes", "GET", "/_nodes/settings,os,jvm?flat_settings=true", None, "nodes", None),
        ("create", "PUT", "/fixture", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {"properties": {"revision": {"type": "long"}, "uid": {"type": "keyword"}, "payload": {"type": "keyword"}}}}, "create", None),
        ("bulk", "POST", "/_bulk?refresh=true", bulk, "bulk", None),
        ("initial", "POST", search, account, "search", 0),
        ("search", "POST", search, {"size": 1, "_source": False, "query": {"match_all": {}}}, "search", None),
    ]
    for name, size, revision in (("ubq-b16", 16, 128), ("ubq-b1-first", 1, 256), ("ubq-b1-repeat", 1, 384)):
        path = f"/fixture/_update_by_query?wait_for_completion=true&slices=1&scroll_size={size}&refresh=true&request_cache=false&conflicts=abort"
        script = {"query": {"match_all": {}}, "script": {"lang": "painless", "source": "ctx._source.revision += params.increment", "params": {"increment": 1}}}
        table.extend(((name, "POST", path, script, "ubq", DOCS // size), (name + "-sum", "POST", search, account, "search", revision)))
    for name, method, path, body, kind, expected in table:
        headers = ["X-Opaque-Id: tiny-" + name]
        if body is not None:
            headers.append("Content-Type: application/" + ("x-ndjson" if kind == "bulk" else "json"))
        yield name, {"method": method, "path": path, "headers": headers, "body": body if isinstance(body, str) or body is None else encode(body)}, kind, expected


def environment(owner):
    return {"cluster.name": owner, "node.name": owner + "-node", "discovery.type": "single-node", "network.host": "0.0.0.0", "node.processors": "2", "OPENSEARCH_JAVA_OPTS": "-Xms512m -Xmx512m", "DISABLE_INSTALL_DEMO_CONFIG": "true", "DISABLE_SECURITY_PLUGIN": "true", "task_resource_tracking.enabled": "true"}


def launch_command(owner):
    args = [*DOCKER, "run", "-d", "--pull", "never", "--name", owner + "-node", "--hostname", owner + "-node", "--label", f"{LABEL}={owner}", "--cpus", "2", "--memory", "2g", "--memory-swap", "2g", "--ulimit", "memlock=-1:-1", "--ulimit", "nofile=65536:65536", "-p", "127.0.0.1::9200", "--mount", f"type=tmpfs,destination={DATA},tmpfs-size=268435456,tmpfs-mode=1777"]
    for key, value in environment(owner).items():
        args.extend(("-e", key + "=" + value))
    return args + [IMAGE]


def owned_listing(owner):
    return [*DOCKER, "ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={owner}", "--filter", "name=" + owner + "-node"]


def owned(container, owner):
    need(container["Config"]["Labels"].get(LABEL) == owner and container["Name"] == "/" + owner + "-node", "container owner/name mismatch")
    need(re.fullmatch(r"[0-9a-f]{64}", container["Id"]), "invalid container ID")


def check_image(image):
    need(re.fullmatch(r"sha256:[0-9a-f]{64}", image["Id"]) and type(image["RepoDigests"]) is list and IMAGE in image["RepoDigests"], "cached pinned image identity")


def geometry(image, container, owner):
    check_image(image)
    owned(container, owner)
    config, host, network = container["Config"], container["HostConfig"], container["NetworkSettings"]
    need(container["Image"] == image["Id"] and config["Image"] == IMAGE, "actual container image mismatch")
    for key in ("Entrypoint", "Cmd", "User", "WorkingDir"):
        need(config[key] == image["Config"][key], "image configuration: " + key)
    env = dict(item.split("=", 1) for item in config["Env"])
    need(len(env) == len(config["Env"]) and env == dict(item.split("=", 1) for item in image["Config"]["Env"]) | environment(owner), "container environment mismatch")
    integers(host, NanoCpus=2000000000, Memory=2147483648, MemorySwap=2147483648)
    need(encode(host["Mounts"]) == encode([{"Type": "tmpfs", "Target": DATA, "TmpfsOptions": {"SizeBytes": 268435456, "Mode": 1023}}]), "bounded tmpfs configuration")
    need(encode(container["Mounts"]) == encode([{"Type": "tmpfs", "Source": "", "Destination": DATA, "Mode": "", "RW": True, "Propagation": ""}]), "actual data mount")
    need(not any(host.get(k) for k in ("Binds", "VolumesFrom", "Tmpfs", "Privileged", "CapAdd", "Devices", "DeviceRequests", "PublishAllPorts")) and not config.get("Volumes"), "unexpected mounts/privileges/devices")
    need(host["NetworkMode"] == "bridge" and set(network["Networks"]) == {"bridge"}, "only default bridge allowed")
    need(host["PortBindings"] == {"9200/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}, "requested loopback mapping")
    ports = {k: v for k, v in network["Ports"].items() if v is not None}
    need(set(ports) == {"9200/tcp"} and len(ports["9200/tcp"]) == 1, "published ports")
    binding = ports["9200/tcp"][0]
    need(binding["HostIp"] == "127.0.0.1" and re.fullmatch(r"[0-9]+", binding["HostPort"]) and 0 < int(binding["HostPort"]) <= 65535, "actual loopback binding")
    return "http://127.0.0.1:" + binding["HostPort"]


def check_response(body, kind, expected, owner):
    if kind == "health":
        need(body["cluster_name"] == owner and body["status"] == "green" and body["timed_out"] is False, "cluster health/identity")
        integers(body, number_of_nodes=1, number_of_data_nodes=1)
    elif kind == "nodes":
        need(body["cluster_name"] == owner and len(body["nodes"]) == 1, "node count/cluster")
        integers(body["_nodes"], total=1, successful=1, failed=0)
        node_id, node = next(iter(body["nodes"].items()))
        need(bool(node_id) and node["version"] == "2.19.0" and node["name"] == owner + "-node", "raw node identity/version")
        integers(node["os"], allocated_processors=2)
        integers(node["jvm"]["mem"], heap_init_in_bytes=HEAP, heap_max_in_bytes=HEAP)
        need(node["settings"]["task_resource_tracking.enabled"] == "true" and node["settings"]["node.processors"] == "2", "raw node settings")
        return node_id
    elif kind == "create":
        need(body["acknowledged"] is True and body["shards_acknowledged"] is True and body["index"] == "fixture", "index creation")
    elif kind == "bulk":
        need(body["errors"] is False and len(body["items"]) == DOCS, "bulk item count/errors")
        for i, item in enumerate(body["items"]):
            need(set(item) == {"index"}, "bulk operation")
            item = item["index"]
            need(item["_index"] == "fixture" and item["_id"] == str(i) and item["result"] == "created" and "error" not in item, "bulk create identity/result")
            integers(item, status=201, _version=1, _seq_no=i, _primary_term=1)
            integers(item["_shards"], total=1, successful=1, failed=0)
            need(item["_shards"].get("failures", []) == [], "bulk shard failures")
    elif kind == "search":
        need(body["timed_out"] is False and body["hits"]["total"]["relation"] == "eq" and body["_shards"].get("failures", []) == [], "search timeout/failures/relation")
        integers(body["hits"]["total"], value=DOCS)
        integers(body["_shards"], total=1, successful=1, skipped=0, failed=0)
        if expected is not None:
            value = body["aggregations"]["revision"]["value"]
            need(type(value) in (int, float) and math.isfinite(value) and value == expected, "revision sum")
    elif kind == "ubq":
        integers(body, total=DOCS, updated=DOCS, batches=expected, deleted=0, noops=0, version_conflicts=0)
        integers(body["retries"], bulk=0, search=0)
        need(set(body["retries"]) == {"bulk", "search"}, "retry fields")
        if "created" in body:
            integers(body, created=0)
        need(body["failures"] == [] and body["timed_out"] is False, "update failures/timeout")


def check_headers(path, expected=None, node_id=None):
    lines = path.read_bytes().splitlines()
    statuses = [line.split()[1] for line in lines if line.startswith(b"HTTP/")]
    need(statuses and statuses[-1] == b"200", f"{path.name}: HTTP status")
    resources = [line.partition(b":")[2] for line in lines if line.partition(b":")[0].lower() == b"task_resource_usage"]
    if expected is not None:
        need(len(resources) == expected, f"{path.name}: literal resource-header count != {expected}")
        for raw in resources:
            item = json.loads(raw)
            need(set(item) == {"action", "taskId", "parentTaskId", "nodeId", "taskResourceUsage"}, "resource shape")
            need(type(item["action"]) is str and bool(item["action"]) and item["nodeId"] == node_id, "resource action/node")
            need(all(type(item[k]) is int for k in ("taskId", "parentTaskId")), "resource task IDs")
            usage = item["taskResourceUsage"]
            need(set(usage) == {"cpu_time_in_nanos", "memory_in_bytes"} and all(type(v) is int and v >= 0 for v in usage.values()), "resource usage shape/values")


def verify_run(directory):
    """Shared live/offline validation, not an independent or authenticated attestation."""
    root = Path(directory)
    try:
        receipt, cleanup = (read_json(root / name) for name in ("receipt.json", "cleanup.json"))
        need(set(receipt) == {"owner", "source_sha256", "execution_ok", "errors"} and receipt["execution_ok"] is True and receipt["errors"] == [], "execution receipt incomplete/failed")
        owner = receipt["owner"]
        need(re.fullmatch(r"run-[0-9a-f]{32}", owner), "owner UUID")
        need(digest(root / "reproduce.py") == receipt["source_sha256"], "source snapshot hash mismatch")
        container = read_json(root / "container.inspect.json")
        geometry(read_json(root / "image.inspect.json"), container, owner)
        need(read_json(root / "launch.json") == {"argv": launch_command(owner)}, "launch mismatch")
        need(cleanup.get("clean") is True and cleanup == {"owner": owner, "clean": True, "errors": [], "removed": [container["Id"]], "remaining": []}, "cleanup failed/unknown/mismatched")
        inspected = read_json(root / "cleanup.inspect.json")
        owned(inspected, owner)
        need(inspected["Id"] == container["Id"], "cleanup container mismatch")
        (root / "container.log").read_bytes()
        commands = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
        successful = [entry["argv"] for entry in commands if type(entry.get("returncode")) is int and entry["returncode"] == 0 and "error" not in entry]
        need(launch_command(owner) in successful and [*DOCKER, "rm", "-f", "-v", container["Id"]] in successful, "missing successful launch/removal transcript")
        final = commands[-1]
        need(final["argv"] == owned_listing(owner) and type(final.get("returncode")) is int and final["returncode"] == 0 and "error" not in final and final.get("stdout", "unknown").strip() == "", "final owned listing not confirmed empty")
        counts, node_id = {}, None
        for name, request, kind, expected in steps():
            need(read_json(root / (name + ".request.json")) == request, name + ": request mismatch")
            result = check_response(read_json(root / (name + ".body")), kind, expected, owner)
            if kind == "nodes":
                node_id = result
            count = expected if kind == "ubq" else (0 if name == "search" else None)
            check_headers(root / (name + ".headers"), count, node_id)
            if count is not None:
                counts[name] = count
        return {"verified": True, "literal_resource_header_counts": counts}
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        raise VerificationError(f"malformed/missing evidence: {exc}") from exc


class Runner:
    def __init__(self, output):
        self.output, self.source = Path(output), Path(__file__).resolve()
        self.owner = "run-" + uuid.uuid4().hex
        self.receipt = {"owner": self.owner, "source_sha256": None, "execution_ok": False, "errors": []}
        self.lock, self.container = None, None
        self.started = self.created_output = self.cleaning = False
        self.deadline = time.monotonic() + WORK

    def check_lock(self):
        a, b = os.fstat(self.lock.fileno()), LOCK.stat()
        need((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), "lock inode changed")

    def command(self, argv, timeout=30, deadline=None):
        if not self.cleaning:
            self.check_lock()
        allowed = min(timeout, (min(self.deadline, deadline) if deadline is not None else self.deadline) - time.monotonic())
        need(allowed > 0, "deadline exceeded")
        record = {"argv": argv, "timeout": allowed}
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=allowed)
            record.update(stdout=result.stdout, stderr=result.stderr, returncode=result.returncode)
            need(result.returncode == 0, f"{argv[0]} failed ({result.returncode}): {result.stderr}")
            return result.stdout.strip()
        except BaseException as exc:
            record["error"] = repr(exc)
            if isinstance(exc, subprocess.TimeoutExpired):
                record.update({key: value.decode(errors="replace") if isinstance(value, bytes) else value for key, value in (("stdout", exc.stdout), ("stderr", exc.stderr))})
            raise
        finally:
            try:
                with (self.output / "commands.jsonl").open("a") as stream:
                    stream.write(encode(record) + "\n")
            except OSError as exc:
                self.receipt["errors"].append("command logging: " + str(exc))

    def http(self, step, deadline=None):
        name, request, _, _ = step
        save(self.output / (name + ".request.json"), request)
        allowed = min(120, min(self.deadline, deadline or self.deadline) - time.monotonic())
        need(allowed > 0, "HTTP deadline exceeded")
        args = ["curl", "-q", "--silent", "--show-error", "--noproxy", "*", "--max-time", str(allowed), "--connect-timeout", str(min(10, allowed)), "-D", str(self.output / (name + ".headers")), "-o", str(self.output / (name + ".body")), "-w", "%{http_code}", "-X", request["method"]]
        for header in request["headers"]:
            args.extend(("-H", header))
        if request["body"] is not None:
            args.extend(("--data-binary", request["body"]))
        need(self.command(args + [self.base + request["path"]], allowed, deadline) == "200", name + ": HTTP status")
        return read_json(self.output / (name + ".body"))

    def prepare(self):
        need(not any(k in os.environ for k in ("DOCKER_HOST", "DOCKER_CONTEXT")), "DOCKER_HOST/DOCKER_CONTEXT overrides refused")
        self.lock = os.fdopen(os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.check_lock()
        self.output.mkdir(parents=True, exist_ok=False)
        self.created_output = True
        (self.output / "reproduce.py").write_bytes(self.source.read_bytes())
        self.receipt["source_sha256"] = digest(self.output / "reproduce.py")
        need(self.command(["docker", "context", "show"]) == "default", "Docker context must be default")
        context = json.loads(self.command([*DOCKER, "context", "inspect", "default"]))
        need(len(context) == 1 and context[0]["Endpoints"]["docker"]["Host"].startswith("unix:///"), "local Unix Docker endpoint required")
        need(not self.command([*DOCKER, "ps", "-aq"]), "existing containers (including stopped) refused")
        try:
            self.image = json.loads(self.command([*DOCKER, "image", "inspect", IMAGE]))[0]
            check_image(self.image)
        except Exception as exc:
            raise VerificationError(f"cached pinned image required; pull manually: docker pull {IMAGE}; {exc}") from exc
        save(self.output / "image.inspect.json", self.image)

    def execute(self):
        args = launch_command(self.owner)
        save(self.output / "launch.json", {"argv": args})
        self.check_lock()
        self.started = True  # Even a timed-out/interrupted run may have created a container.
        ident = self.command(args)
        need(re.fullmatch(r"[0-9a-f]{64}", ident), "invalid launch ID")
        self.container = ident
        container = json.loads(self.command([*DOCKER, "inspect", ident]))[0]
        need(container["Id"] == ident, "inspect/launch identity mismatch")
        save(self.output / "container.inspect.json", container)
        self.base = geometry(self.image, container, self.owner)
        plan, ready, attempt = list(steps()), min(self.deadline, time.monotonic() + READY), 0
        while True:
            try:
                check_response(self.http(plan[0], ready), "health", None, self.owner)
                break
            except Interrupted:
                raise
            except Exception as exc:
                for path in self.output.glob("health.*"):
                    path.rename(path.with_name(f"ready-{attempt}-" + path.name))
                (self.output / f"ready-{attempt}.error").write_text(str(exc))
                left = ready - time.monotonic()
                need(left > 0, "readiness deadline exceeded: " + str(exc))
                time.sleep(min(1, left))
                attempt += 1
        node_id = check_response(self.http(plan[1], ready), "nodes", None, self.owner)
        for step in plan[2:]:
            name, _, kind, expected = step
            check_response(self.http(step), kind, expected, self.owner)
            check_headers(self.output / (name + ".headers"), expected if kind == "ubq" else (0 if name == "search" else None), node_id)

    def cleanup(self):
        self.cleaning, self.deadline = True, time.monotonic() + CLEANUP
        errors, removed, ids, remaining = [], [], [], None
        def attempt(action):
            try:
                return action()
            except Exception as exc:
                errors.append(str(exc))
        attempt(self.check_lock)
        listing = owned_listing(self.owner)
        if self.started:
            found = attempt(lambda: self.command(listing))
            ids = (found or "").splitlines()
            if self.container and self.container not in ids:
                ids.append(self.container)
            if not ids:
                errors.append("launch outcome unknown: no owned container recovered")
            for ident in ids:
                def inspect():
                    need(re.fullmatch(r"[0-9a-f]{64}", ident), "invalid recovery ID")
                    obj = json.loads(self.command([*DOCKER, "inspect", ident]))[0]
                    owned(obj, self.owner)
                    need(obj["Id"] == ident, "cleanup inspect ID mismatch")
                    return obj
                obj = attempt(inspect)
                if obj is None:
                    continue
                attempt(lambda: save(self.output / "cleanup.inspect.json", obj))
                attempt(lambda: (self.output / "container.log").write_text(self.command([*DOCKER, "logs", ident], timeout=10)))
                if attempt(lambda: self.command([*DOCKER, "rm", "-f", "-v", ident])) is not None:
                    removed.append(ident)
            found = attempt(lambda: self.command(listing))
            remaining = found.splitlines() if found is not None else None
        else:
            remaining = []
        need_clean = not errors and remaining == [] and (not self.started or len(removed) == 1)
        save(self.output / "cleanup.json", {"owner": self.owner, "clean": need_clean, "errors": errors, "removed": removed, "remaining": remaining})
        need(need_clean, "cleanup failed/unknown: " + repr(errors))

    def run(self):
        def stop(signum, _frame):
            if not self.cleaning:
                self.cleaning = True
                raise Interrupted(f"interrupted by signal {signum}")
        handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.prepare()
            self.execute()
            self.receipt["execution_ok"] = True
        except Exception as exc:
            self.receipt["errors"].append(str(exc))
        finally:
            self.cleaning = True  # Repeated signals cannot interrupt bounded cleanup.
            try:
                if self.created_output:
                    try:
                        self.cleanup()
                        need(digest(self.source) == self.receipt["source_sha256"], "running source changed")
                    except Exception as exc:
                        self.receipt["errors"].append(str(exc))
                    save(self.output / "receipt.json", self.receipt)
            finally:
                if self.lock is not None:
                    self.lock.close()
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
        need(not self.receipt["errors"], "; ".join(self.receipt["errors"]))
        return verify_run(self.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path, help="run into a NEW directory")
    group.add_argument("--verify", type=Path, help="verify saved evidence offline (no Docker)")
    args = parser.parse_args(argv)
    try:
        need(args.output is None or not args.output.exists(), "output already exists")
        result = verify_run(args.verify) if args.verify is not None else Runner(args.output).run()
    except Exception as exc:
        result = {"verified": False, "error": str(exc)}
    print(encode(result))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
