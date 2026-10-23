"""Decode the input snapshots consumed by the native SAV player.

See docs/decisions/0017-replay-input-snapshots.md for the static evidence.
These are logical input channels, not Windows keyboard event timestamps.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import struct
from pathlib import Path


CONTRACT = "replay_input_snapshots_v1"
STRIDE = 230
# File byte -> CRecord MoveInfo member -> playback input-state member.
FIELDS = (
    ("left", 60, 68, 3),
    ("right", 61, 69, 2),
    ("up", 62, 70, 0),
    ("down", 63, 71, 1),
    ("drift", 64, 72, 4),
    ("nitro", 65, 73, 5),
    ("reset", 66, 74, 6),
    ("up_secondary", 67, 75, 7),
)
CONTROL_NAMES = tuple(field[0] for field in FIELDS)
FRAME_COLUMNS = ("record_index", "time_ms", "time_s", "file_offset", *CONTROL_NAMES)
EVENT_COLUMNS = (
    "record_index", "previous_time_ms", "time_ms", "control", "transition",
    "state", "file_offset",
)
INTERVAL_COLUMNS = (
    "control", "start_ms", "end_ms", "observed_duration_ms", "start_record_index",
    "end_record_index", "start_kind", "end_kind", "start_censored", "end_censored",
)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def native_bounds(data, meta):
    """Recover count-framed records preceding the detector's first usable pose.

    The physical detector can omit the initial frame when the next timestamp is
    more than 100 ms later. The native count and the detected end must agree
    exactly; a pose heuristic alone never establishes an input table boundary.
    """
    if meta.get("profile") != "2026":
        raise ValueError("unsupported physical profile; input labels remain unavailable")
    version = struct.unpack_from("<I", data)[0] if len(data) >= 4 else -1
    if not 50 <= version <= 55:
        raise ValueError("unsupported SAV version; verified layout is version 50..55 / stride 230")
    start, count = int(meta["byte_start"]), int(meta["records"])
    if count < 2 or start < 4 or start + count * STRIDE > len(data):
        raise ValueError("physical stream is truncated or has invalid bounds")
    if int(meta["byte_end"]) != start + (count - 1) * STRIDE:
        raise ValueError("physical manifest end does not match its record count")
    matches = []
    for prefix in range(min(32, (start - 4) // STRIDE) + 1):
        candidate = start - prefix * STRIDE
        declared = struct.unpack_from("<I", data, candidate - 4)[0]
        if declared == count + prefix:
            matches.append((candidate, declared, prefix))
    if len(matches) != 1:
        raise ValueError("native MoveInfo count boundary is missing or ambiguous")
    return version, matches[0]


def decode_stream(data, meta):
    version, (start, count, prefix) = native_bounds(data, meta)
    frames, events, intervals = [], [], []
    active = {}
    previous = None
    duplicate_times = 0
    max_gap = 0
    for index in range(count):
        offset = start + index * STRIDE
        time_ms = struct.unpack_from("<I", data, offset)[0]
        values = tuple(data[offset + field[1]] for field in FIELDS)
        if any(value not in (0, 1) for value in values):
            raise ValueError(f"non-boolean native input at record {index}; labels were not published")
        if time_ms > 600000 or (previous is not None and time_ms < previous["time_ms"]):
            raise ValueError("invalid or decreasing native input clock")
        if previous is not None:
            gap = time_ms - previous["time_ms"]
            duplicate_times += gap == 0
            max_gap = max(max_gap, gap)
        row = dict(record_index=index, time_ms=time_ms, time_s=time_ms / 1000, file_offset=offset)
        row.update(zip(CONTROL_NAMES, values))
        frames.append(row)
        for name, relative, _, _ in FIELDS:
            state = row[name]
            changed = previous is not None and state != previous[name]
            if changed or (previous is None and state):
                kind = "initial" if previous is None else "press" if state else "release"
                events.append(dict(record_index=index, previous_time_ms="" if previous is None else previous["time_ms"],
                                   time_ms=time_ms, control=name, transition=kind, state=state,
                                   file_offset=offset + relative))
            if state and (previous is None or changed):
                active[name] = (index, time_ms, previous is None)
            if not state and changed:
                begin_index, begin_ms, censored = active.pop(name)
                intervals.append(dict(control=name, start_ms=begin_ms, end_ms=time_ms,
                                      observed_duration_ms=time_ms-begin_ms, start_record_index=begin_index,
                                      end_record_index=index, start_kind="initial" if censored else "press",
                                      end_kind="release", start_censored=int(censored), end_censored=0))
        previous = row
    for name, (begin_index, begin_ms, censored) in active.items():
        intervals.append(dict(control=name, start_ms=begin_ms, end_ms=frames[-1]["time_ms"],
                              observed_duration_ms=frames[-1]["time_ms"]-begin_ms,
                              start_record_index=begin_index, end_record_index=count-1,
                              start_kind="initial" if censored else "press", end_kind="recording_end",
                              start_censored=int(censored), end_censored=1))
    intervals.sort(key=lambda row: (row["start_record_index"], CONTROL_NAMES.index(row["control"])))
    report = dict(container_version=version, stride=STRIDE, native_count_offset=start-4,
                  byte_start=start, byte_end_exclusive=start+count*STRIDE, frame_count=count,
                  restored_prefix_frames=prefix, boundary="native_count_and_detected_end_agree",
                  time_start_ms=frames[0]["time_ms"], time_end_ms=frames[-1]["time_ms"],
                  duplicate_clock_rows=duplicate_times, max_sample_gap_ms=max_gap,
                  controls={name: dict(pressed_frames=sum(row[name] for row in frames),
                                       presses=sum(e["control"] == name and e["transition"] == "press" for e in events),
                                       releases=sum(e["control"] == name and e["transition"] == "release" for e in events))
                            for name in CONTROL_NAMES})
    return frames, events, intervals, report


def write_csv(path, columns, rows):
    # BOM allows ordinary Windows Excel to recognize UTF-8 reliably.
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def export_sources(sources, destination):
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("output directory is not empty; existing exports are preserved")
    prepared, seen = [], set()
    for source in sources:
        path = Path(source["replay_path"])
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != source["sha256"]:
            raise ValueError("SAV content changed before input extraction")
        if digest in seen:
            continue
        seen.add(digest)
        raw = read_json(source["physical_manifest"])
        replays = raw.get("replays", [])
        if len(replays) != 1 or replays[0].get("source_size_bytes") != len(data):
            raise ValueError("physical manifest does not describe this SAV")
        streams = replays[0].get("streams", [])
        # A remote opponent may carry placeholder zeroes instead of input. Do
        # not turn that absence into supervised 'released' keyboard labels.
        if len(streams) != 1:
            raise ValueError("multiple/fragmented vehicle streams require ownership verification before input export")
        meta = streams[0]
        hz = float(meta.get("approx_sample_hz", 0))
        if not math.isfinite(hz) or hz < 40:
            raise ValueError("low-frequency vehicle stream has no verified local keyboard ownership")
        result = decode_stream(data, meta)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("SAV content changed during input extraction")
        prepared.append((digest, path.name, result))
    if not prepared:
        raise ValueError("no replay inputs")
    # Validate every source before writing any result files.
    destination.mkdir(parents=True, exist_ok=True)
    entries, index_rows = [], []
    for digest, filename, (frames, events, intervals, report) in prepared:
        folder = destination / digest
        folder.mkdir()
        write_csv(folder / "frames.csv", FRAME_COLUMNS, frames)
        write_csv(folder / "events.csv", EVENT_COLUMNS, events)
        write_csv(folder / "intervals.csv", INTERVAL_COLUMNS, intervals)
        entry = dict(sha256=digest, stream_id="stream_01", source_scope="single_high_frequency_vehicle",
                     frames_file=f"{digest}/frames.csv", events_file=f"{digest}/events.csv",
                     intervals_file=f"{digest}/intervals.csv", event_count=len(events), interval_count=len(intervals), **report)
        write_json(folder / "summary.json", entry)
        entries.append(entry)
        index_rows.append(dict(replay_file=filename, sha256=digest, frames=len(frames),
                               events=len(events), frames_csv=entry["frames_file"],
                               events_csv=entry["events_file"], intervals_csv=entry["intervals_file"]))
    write_csv(destination / "replay_index.csv",
              ("replay_file", "sha256", "frames", "events", "frames_csv", "events_csv", "intervals_csv"), index_rows)
    manifest = dict(contract=CONTRACT, control_names=CONTROL_NAMES,
                    fields=[dict(name=n, file_offset=o, move_info_offset=m, playback_input_offset=k) for n,o,m,k in FIELDS],
                    semantics=dict(values="1=pressed, 0=released; native logical input snapshots",
                                   time="native recording clock in milliseconds; no race-time subtraction",
                                   transitions="observed at sample time; actual edge lies after previous_time_ms and at/before time_ms",
                                   first_frame="active inputs are initial observations, never invented presses",
                                   last_frame="held intervals are censored at recording end, never invented releases",
                                   up_secondary="second binding of forward input; stored independently from up",
                                   physical_keys="original physical bindings and sub-frame events are unavailable"),
                    replays=entries, totals=dict(replays=len(entries), frames=sum(e["frame_count"] for e in entries),
                                                events=sum(e["event_count"] for e in entries)))
    write_json(destination / "manifest.json", manifest)
    return manifest
