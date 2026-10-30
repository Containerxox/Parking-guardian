"""Compare two `device/pc_event_test.py --out` results (for example Prototype vs Phase 1).

    python backend/scripts/compare_pc_event_runs.py prototype.json phase1.json [--labels Prototype "Phase 1"]

Compared: the events the device sent and the answers it got, in order (image, event, server result,
episode_ended, ok), the device state changes, the number of heartbeats and the LED at the end.
NOT compared, because they differ between any two runs: simulated times, milliseconds, photo sizes, database ids.
The exit code is 1 when the runs differ.
"""
import argparse
import json
import sys


def event_signature(run: dict) -> list[tuple]:
    return [(e["image"], e["event"], e["result"], bool(e["episode_ended"]), bool(e["ok"])) for e in run["events"]]


def transition_signature(run: dict) -> list:
    return [state for _, state in run["state_transitions"]]


def compare(a: dict, b: dict) -> dict:
    sig_a, sig_b = event_signature(a), event_signature(b)
    differences = [(i, x, y) for i, (x, y) in enumerate(zip(sig_a, sig_b)) if x != y]
    same = (not differences and len(sig_a) == len(sig_b)
            and transition_signature(a) == transition_signature(b)
            and a["heartbeats"] == b["heartbeats"] and a["led_on_at_end"] == b["led_on_at_end"])
    return {"identical": same, "event_count": (len(sig_a), len(sig_b)), "event_differences": differences,
            "transitions": (transition_signature(a), transition_signature(b)),
            "heartbeats": (a["heartbeats"], b["heartbeats"]), "led_on_at_end": (a["led_on_at_end"], b["led_on_at_end"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first")
    parser.add_argument("second")
    parser.add_argument("--labels", nargs=2, default=["first", "second"])
    args = parser.parse_args()
    a, b = (json.load(open(path, encoding="utf-8")) for path in (args.first, args.second))
    la, lb = args.labels
    print(f"| # | event | {la}: server result | {lb}: server result | episode_ended | same |\n|---|---|---|---|---|---|")
    for i in range(max(len(a["events"]), len(b["events"]))):
        ea = a["events"][i] if i < len(a["events"]) else None
        eb = b["events"][i] if i < len(b["events"]) else None
        sig = lambda e: None if e is None else (e["image"], e["event"], e["result"], bool(e["episode_ended"]), bool(e["ok"]))
        name = (ea or eb)["event"]
        print(f"| {i + 1} | {name} | {ea['result'] if ea else '-'} (sim {ea['sim_s'] if ea else '-'} s) "
              f"| {eb['result'] if eb else '-'} (sim {eb['sim_s'] if eb else '-'} s) "
              f"| {ea['episode_ended'] if ea else '-'} / {eb['episode_ended'] if eb else '-'} | {sig(ea) == sig(eb)} |")
    result = compare(a, b)
    print(f"\n- device state changes: {la} {result['transitions'][0]} / {lb} {result['transitions'][1]}")
    print(f"- heartbeats: {result['heartbeats'][0]} / {result['heartbeats'][1]}; LED on at end: {result['led_on_at_end']}")
    print(f"- frames: {a['frames']} / {b['frames']}; mean detection ms: {a['detect_ms_mean']} / {b['detect_ms_mean']}")
    print(f"- **identical (events, answers, state changes, heartbeats, LED): {result['identical']}**")
    return 0 if result["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
