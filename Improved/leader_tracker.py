# Leader tracker for the failover test (experiment 2 in ASSIGNMENT.md).
# Watches the bully pods through kubectl and measures how long it takes from the leader pod being
# gone until a new leader has been elected. The leader is the pod labelled role=leader (see set_leader_label in improved.py).
#
# Requires Python 3.9+ and kubectl (no pip installs). Run from the repo root in its own terminal:
#   python3 Improved/leader_tracker.py
#   python3 Improved/leader_tracker.py --out Improved/results/my-run.csv
# Then scale in another terminal, e.g. kubectl scale statefulset bully --replicas=2
# Scaling down always removes the highest pod, which is the leader. Stop the tracker with Ctrl+C.
#
# Where to read the results:
#   1. The terminal: one line per event and a summary when you press Ctrl+C.
#   2. Improved/results/leader-tracker-<time>.csv: one row per leader change.
#      event          failover = the leader pod went down, scale_up = a new, higher pod took over
#      start_at       failover: when the old leader pod was fully gone. scale_up: when the new pod was created
#      terminating_at when the old leader was marked Terminating (just after the scale command), for context
#      new_leader_at  when the new leader had labelled itself role=leader
#      election_s     new_leader_at - start_at in seconds
#   All times are UTC, so they line up with kubectl logs --timestamps.

import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---- Settings (override with --name value) ----
def parse_args(argv):
    out = {}
    for i in range(0, len(argv) - 1, 2):
        out[argv[i].lstrip("-")] = argv[i + 1]
    return out

ARGS = parse_args(sys.argv[1:])
STARTED = datetime.now(timezone.utc)
OUT_FILE = ARGS.get("out") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "results",
    f"leader-tracker-{STARTED.strftime('%Y-%m-%dT%H-%M-%S')}.csv",
)
KUBECTL = ["kubectl", "get", "pods", "-l", "app=bully", "--watch", "--output-watch-events", "-o", "json"]
COLUMNS = ["event", "old_leader", "new_leader", "start_at", "terminating_at", "new_leader_at", "election_s", "pods_alive"]

# ---- State ----
pods = {}         # pod name -> {"role", "terminating_at", "created_at"}
leader = None     # Last known leader
pending = None    # Failover waiting for a new leader: {"old", "gone_at", "terminating_at"}
early = None      # New leader that appeared before the old leader's pod was gone (rare)
failover_times = []


def now():
    return datetime.now(timezone.utc)

def iso(t):
    return t.isoformat(timespec="milliseconds") if t else ""

def local(t):
    return t.astimezone().strftime("%H:%M:%S.%f")[:-3]

def ordinal(name):
    return int(name.rsplit("-", 1)[-1])

def alive_count():
    return sum(1 for p in pods.values() if not p["terminating_at"])

def current_leader():
    # Non-terminating pod with role=leader; highest id wins if two briefly overlap during an election
    candidates = [n for n, p in pods.items() if p["role"] == "leader" and not p["terminating_at"]]
    return max(candidates, key=ordinal) if candidates else None


# ---- CSV output ----
os.makedirs(os.path.dirname(os.path.abspath(OUT_FILE)), exist_ok=True)
csv_file = open(OUT_FILE, "w", newline="")
writer = csv.writer(csv_file)
writer.writerow(COLUMNS)
csv_file.flush()

def write_row(event, old, new, start_at, terminating_at, new_leader_at):
    election_s = (new_leader_at - start_at).total_seconds() if start_at else None
    writer.writerow([event, old or "", new, iso(start_at), iso(terminating_at), iso(new_leader_at),
                     f"{election_s:.3f}" if election_s is not None else "", alive_count()])
    csv_file.flush()  # Written at once so Ctrl+C loses nothing
    if event == "failover" and election_s is not None:
        failover_times.append(election_s)
    took = f" after {election_s:.3f} s" if election_s is not None else ""
    print(f"{local(new_leader_at)}  [{event.upper()}] {old} -> {new}{took} ({alive_count()} pods alive)")


# ---- Event handling ----
def handle(event_type, pod, t):
    global leader, pending, early
    name = pod["metadata"]["name"]

    if event_type == "DELETED":
        info = pods.pop(name, None)
        terminating_at = info["terminating_at"] if info else None
        if early and early["old"] == name:
            # New leader was already in place before the old pod was gone: duration comes out negative
            print(f"WARNING: {early['new']} became leader before {name} was gone")
            write_row("failover", name, early["new"], t, terminating_at, early["at"])
            early = None
        elif name == leader:
            pending = {"old": name, "gone_at": t, "terminating_at": terminating_at}
            print(f"{local(t)}  [LEADER GONE] {name}, waiting for new leader...")
    else:  # ADDED / MODIFIED
        meta = pod["metadata"]
        if name not in pods:
            created = datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00"))
            # Pods created after the tracker started get the precise time we saw them; older ones keep the API time
            pods[name] = {"role": None, "terminating_at": None, "created_at": t if created >= STARTED.replace(microsecond=0) else created}
        p = pods[name]
        p["role"] = (meta.get("labels") or {}).get("role")
        if meta.get("deletionTimestamp") and not p["terminating_at"]:
            p["terminating_at"] = t
            if name == leader:
                print(f"{local(t)}  [LEADER TERMINATING] {name}")

    new = current_leader()
    if new is None or new == leader:
        return

    if leader is None:
        print(f"{local(t)}  [CURRENT LEADER] {new} ({alive_count()} pods alive)")
    elif pending:
        write_row("failover", pending["old"], new, pending["gone_at"], pending["terminating_at"], t)
        pending = None
    elif leader in pods and pods[leader]["terminating_at"]:
        early = {"old": leader, "new": new, "at": t}  # Row is written when the old pod's DELETED event arrives
    else:
        write_row("scale_up", leader, new, pods[new]["created_at"], None, t)
    leader = new


def watch():
    # kubectl prints one JSON event per line: {"type": "ADDED"|"MODIFIED"|"DELETED", "object": <pod>}
    proc = subprocess.Popen(KUBECTL, stdout=subprocess.PIPE, text=True, bufsize=1)
    started = time.monotonic()
    buffer = ""
    try:
        for line in proc.stdout:
            buffer += line
            try:
                event = json.loads(buffer)
            except json.JSONDecodeError:
                continue  # Event split over several lines; wait for the rest
            buffer = ""
            handle(event["type"], event["object"], now())
    finally:
        proc.terminate()
        proc.wait()
    return proc.returncode, time.monotonic() - started


def summary():
    print(f"\n=== {len(failover_times)} failover(s) ===")
    if failover_times:
        print(f"min {min(failover_times):.3f} s, avg {sum(failover_times) / len(failover_times):.3f} s, "
              f"max {max(failover_times):.3f} s")
    print(f"CSV: {OUT_FILE}")


def main():
    # Ctrl+C and kill both stop the tracker cleanly (a process started in the background ignores Ctrl+C by default)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    print(f"Watching bully pods, writing to {OUT_FILE}\nPress Ctrl+C to stop.\n")
    try:
        while True:
            code, ran_for = watch()
            if code != 0 and ran_for < 3:
                print("kubectl failed; is Docker/Kubernetes running and the cluster deployed?")
                break
            # The API server closes long-running watches; reconnect and keep the state
    except KeyboardInterrupt:
        pass
    finally:
        csv_file.close()
        summary()


if __name__ == "__main__":
    main()
