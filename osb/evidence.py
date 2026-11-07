"""Fail-closed execution checks shared by the live owner and offline analyzer."""
import math


def validate_completion(receipt, events, metrics):
    def need(ok, message):
        if not ok:
            raise ValueError(message)
    checks = [e for e in events if e["kind"] == "correctness"]
    docs = receipt["documents"]
    revision = 512 if receipt["smoke"] else 0 if receipt["control"] else docs
    need([e["revision_sum"] for e in checks] == [0, revision] and
         all(e["documents"] == docs for e in checks), "Incorrect final corpus/revision total")
    timings = [r for r in metrics if r["name"] == "service_time"]
    need(timings and all(r.get("meta", {}).get("success") is True and
                        not r.get("meta", {}).get("timed_out") and
                        math.isfinite(r["value"]) and r["value"] >= 0 for r in timings),
         "Missing/failed OSB operation (including executor timeout)")
    if not receipt["smoke"]:
        prefix = "control" if receipt["control"] else "ubq"
        starts, ends = ([e for e in events if e["kind"] == prefix + "-" + suffix]
                        for suffix in ("start", "end"))
        need(len(starts) == len(ends) == 1, "Missing/duplicate load boundaries")
        need(checks[0]["time"] < starts[0]["time"] < ends[0]["time"] < checks[1]["time"],
             "Invalid lifecycle ordering")
        duration = ends[0]["time"] - starts[0]["time"]
        if receipt["control"]:
            expected = 40 if receipt.get("quick") else 100
            need(expected <= duration < expected + 5, "Truncated/overlong control window")
        elif receipt.get("quick") and docs == 4800:
            need(duration > 35, "Boundary smoke did not cross former 30-second timeout")
