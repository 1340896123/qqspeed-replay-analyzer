"""Training datasets built from production replay telemetry, never inferred key presses."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


CONTRACT = "autodrive_dataset_v1"
STATE_FIELDS = (
    "x", "y", "z", "speed", "speed_raw", "speed_direct", "speed_derived",
    "yaw", "slip", "distance", "vehicle_forward_heading_rad", "contact_state",
    "is_airborne", "lap_index", "system_drift_state", "nitro_active",
    "small_boost_active", "air_boost_active", "landing_boost_active",
    "map_propulsion_active", "small_boost_class_active",
)
EFFECT_FIELDS = STATE_FIELDS[14:]
FEATURES = (
    "relative_x", "relative_y", "relative_z", "velocity_x", "velocity_y",
    "velocity_z", "speed", "yaw_sin", "yaw_cos", "forward_sin", "forward_cos",
    *(f"contact_{i}" for i in range(6)), "is_airborne", "lap_index", *EFFECT_FIELDS,
)
EVENT_CODES = (8, 9, 19, 24, 25)
EVENT_NAMES = ("air_boost", "landing_boost", "wcw_marker", "combo_marker", "cw_marker")
CONTROL_NAMES = ("up", "down", "left", "right", "drift", "nitro")


@dataclass(frozen=True)
class WindowConfig:
    hz: float = 20.0
    history: int = 10
    future: int = 20
    stride: int = 5
    max_gap_s: float = 0.15
    seed: int = 42
    include_network: bool = False

    def validate(self):
        if not math.isfinite(self.hz) or not 1 <= self.hz <= 240:
            raise ValueError("hz must be finite and between 1 and 240")
        if not (2 <= self.history <= 1000 and 1 <= self.future <= 1000 and self.stride >= 1):
            raise ValueError("invalid history, future or stride")
        if not math.isfinite(self.max_gap_s) or not 0 < self.max_gap_s <= 1:
            raise ValueError("max_gap_s must be finite and between 0 and 1")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def numeric(value):
    if value is None or str(value).strip().lower() in ("", "unknown", "null"):
        return np.nan
    if str(value).lower() in ("true", "false"):
        return float(str(value).lower() == "true")
    try:
        result = float(value)
        return result if math.isfinite(result) else np.nan
    except (TypeError, ValueError):
        return np.nan


def safe_child(root, relative):
    """Cached manifests may only reference files inside their own directory."""
    root = Path(root).resolve()
    child = (root / str(relative).replace("\\", "/")).resolve()
    if not child.is_relative_to(root) or child == root:
        raise ValueError("manifest path escapes its source directory")
    return child


def split_replays(hashes, seed):
    """Whole-file split; duplicate contents cannot leak into another split."""
    ordered = sorted(set(hashes), key=lambda h: hashlib.sha256(f"{seed}:{h}".encode()).hexdigest())
    n = len(ordered)
    test_count = max(1, round(n * 0.1)) if n >= 3 else 0
    val_count = max(1, round(n * 0.1)) if n >= 2 else 0
    train_count = n - val_count - test_count
    return {h: "train" if i < train_count else "val" if i < n-test_count else "test"
            for i, h in enumerate(ordered)}


def load_stream(path, meta):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"time_s", "x", "y", "z", "pose_valid", "pose_break_before"} <= rows[0].keys():
        raise ValueError("logical telemetry is empty or missing required quality columns")
    t = np.array([numeric(r.get("time_s")) for r in rows], dtype=np.float64)
    values = np.array([[numeric(r.get(k)) for k in STATE_FIELDS] for r in rows], dtype=np.float64)
    mask = np.isfinite(values)
    valid = np.array([numeric(r.get("pose_valid")) == 1 for r in rows])
    breaks = np.array([numeric(r.get("pose_break_before")) != 0 for r in rows])
    # False defaults in legacy CSVs are NOT observations of an absent effect.
    drift_available = bool(meta.get("system_drift_state_available")) and meta.get("role") == "local_high_frequency"
    effects_available = bool(meta.get("speed_effect_state_available")) and meta.get("role") == "local_high_frequency"
    mask[:, STATE_FIELDS.index("system_drift_state")] &= drift_available
    for name in EFFECT_FIELDS[1:]:
        mask[:, STATE_FIELDS.index(name)] &= effects_available
    nitro_validation = meta.get("native_speed_effect_timeline_validation") or {}
    mask[:, STATE_FIELDS.index("nitro_active")] &= bool(nitro_validation.get("nitro_semantic_valid"))
    contact = values[:, STATE_FIELDS.index("contact_state")]
    mask[:, STATE_FIELDS.index("contact_state")] &= np.isin(contact, range(6))
    mask[:, STATE_FIELDS.index("lap_index")] &= values[:, STATE_FIELDS.index("lap_index")] >= 1
    for name in ("is_airborne", *EFFECT_FIELDS):
        mask[:, STATE_FIELDS.index(name)] &= np.isin(values[:, STATE_FIELDS.index(name)], (0, 1))
    candidates = np.array([[numeric(r.get(f"input_bool_candidate_{k}")) for k in range(60, 66)] for r in rows])
    candidate_mask = np.isin(candidates, (0, 1))
    candidates = np.where(np.isfinite(candidates), candidates, -1).astype(np.int16)
    events = meta.get("native_action_event_timeline") or []
    event_t = np.array([numeric(e.get("time_ms"))/1000 for e in events], dtype=np.float64)
    event_codes = np.array([int(e["action_code"]) for e in events], dtype=np.int32)
    event_available = bool(meta.get("native_action_event_available")) and meta.get("role") == "local_high_frequency"
    production = meta.get("production_actions") or {}
    event_semantics = event_available and bool(production.get("game_facing_available", False))
    return dict(time=t, values=values, mask=mask, pose_valid=valid, breaks=breaks,
                source_stream=np.array([r.get("source_stream", "") for r in rows]),
                candidates=candidates, candidate_mask=candidate_mask,
                event_time=event_t, event_codes=event_codes,
                event_available=event_available, event_semantics=event_semantics)


def valid_segments(data, max_gap):
    """Keep the LAST row at a repeated clock; never bridge a break or invalid row."""
    t, values = data["time"], data["values"]
    result, current = [], []
    duplicates = invalid = 0
    for i in range(len(t)):
        good = bool(data["pose_valid"][i] and np.isfinite(t[i]) and np.isfinite(values[i, :3]).all())
        if not good:
            if current:
                result.append(np.array(current, dtype=np.int64))
            current = []
            invalid += 1
            continue
        split = bool(data["breaks"][i])
        if current:
            prev = current[-1]
            dt = t[i] - t[prev]
            split |= dt < 0 or dt > max_gap or data["source_stream"][i] != data["source_stream"][prev]
            if dt == 0 and not split:
                current[-1] = i
                duplicates += 1
                continue
        if split and current:
            result.append(np.array(current, dtype=np.int64))
            current = []
        current.append(i)
    if current:
        result.append(np.array(current, dtype=np.int64))
    return result, {"duplicate_clock_rows": duplicates, "invalid_pose_rows": invalid,
                    "continuous_segments": len(result)}


def frame_features(data, ids):
    """Causal features: derive velocity backwards, never use central-difference speed/slip."""
    state, masks = data["values"][ids], data["mask"][ids]
    n = len(ids)
    x = np.zeros((n, len(FEATURES)), dtype=np.float64)
    m = np.zeros_like(x, dtype=bool)
    x[:, :3] = state[:, :3]
    m[:, :3] = True
    if n > 1:
        velocity = np.diff(state[:, :3], axis=0) / np.diff(data["time"][ids])[:, None]
        x[1:, 3:6] = velocity
        m[1:, 3:6] = np.isfinite(velocity)
        x[1:, 6] = np.linalg.norm(velocity, axis=1)
        m[1:, 6] = np.isfinite(x[1:, 6])
    direct = STATE_FIELDS.index("speed_direct")
    # The driver only promotes native velocity on the validated modern schema.
    # Older profile direct vectors remain in the physical evidence export.
    native = data.get("native_speed", False) & masks[:, direct]
    x[native, 6] = state[native, direct]
    m[native, 6] = True
    for field, cols in (("yaw", (7, 8)), ("vehicle_forward_heading_rad", (9, 10))):
        k = STATE_FIELDS.index(field)
        x[:, cols[0]] = np.sin(state[:, k])
        x[:, cols[1]] = np.cos(state[:, k])
        m[:, list(cols)] = masks[:, k, None]
    k = STATE_FIELDS.index("contact_state")
    for c in range(6):
        x[:, 11+c] = state[:, k] == c
        m[:, 11+c] = masks[:, k]
    for name in ("is_airborne", "lap_index", *EFFECT_FIELDS):
        a, b = STATE_FIELDS.index(name), FEATURES.index(name)
        x[:, b], m[:, b] = state[:, a], masks[:, a]
    return np.where(m, x, 0), m


def build_windows(data, config):
    config.validate()
    segments, quality = valid_segments(data, config.max_gap_s)
    collected = {k: [] for k in (
        "history", "history_mask", "history_time_s", "history_rows", "anchor_time_s", "origin_xyz",
        "future_position_delta", "future_rows_left", "future_rows_right", "future_effects", "future_effect_mask",
        "future_events", "future_event_mask",
    )}
    dt = 1/config.hz
    history_offsets = np.arange(1-config.history, 1)*dt
    future_offsets = np.arange(1, 1+config.future)*dt
    for ids in segments:
        if len(ids) < 2:
            continue
        t = data["time"][ids]
        feature, feature_mask = frame_features(data, ids)
        first = t[0] - history_offsets[0]
        last = t[-1] - future_offsets[-1]
        count = max(0, int(np.floor((last-first)*config.hz/config.stride + 1e-8))+1)
        for anchor in first + np.arange(count)*config.stride*dt:
            # History is sampled with as-of joins (no future interpolation into observations).
            hquery = anchor + history_offsets
            hi = np.searchsorted(t, hquery + 1e-9, side="right") - 1
            if np.any(hi < 0) or np.any(hquery-t[hi] > config.max_gap_s+1e-9):
                continue
            fquery = anchor + future_offsets
            right = np.searchsorted(t, fquery, side="left")
            if np.any(right >= len(t)):
                continue
            left = np.maximum(0, right-1)
            exact = np.abs(t[right]-fquery) < 1e-8
            left[exact] = right[exact]
            span = t[right]-t[left]
            if np.any(span > config.max_gap_s+1e-9):
                continue
            ratio = np.divide(fquery-t[left], span, out=np.zeros_like(span), where=span > 0)
            pos = data["values"][ids, :3]
            target = pos[left] + ratio[:, None]*(pos[right]-pos[left])
            origin = pos[hi[-1]]
            history = feature[hi].copy()
            history[:, :3] -= origin
            future_ids = np.searchsorted(t, fquery+1e-9, side="right")-1
            ei = [STATE_FIELDS.index(k) for k in EFFECT_FIELDS]
            effects = data["values"][ids[future_ids]][:, ei]
            effect_mask = data["mask"][ids[future_ids]][:, ei]
            event_values = np.zeros((config.future, len(EVENT_CODES)), dtype=np.float32)
            boundaries = np.r_[anchor, fquery]
            for k, code in enumerate(EVENT_CODES):
                et = data["event_time"][data["event_codes"] == code]
                event_values[:, k] = [np.any((et > lo) & (et <= end)) for lo, end in zip(boundaries[:-1], boundaries[1:])]
            values = dict(history=history, history_mask=feature_mask[hi],
                          history_time_s=t[hi]-anchor, history_rows=ids[hi], anchor_time_s=anchor,
                          origin_xyz=origin, future_position_delta=target-origin,
                          future_rows_left=ids[left], future_rows_right=ids[right],
                          future_effects=np.where(effect_mask, effects, 0), future_effect_mask=effect_mask,
                          future_events=event_values,
                          future_event_mask=np.full_like(event_values, data["event_semantics"], dtype=bool))
            for k, v in values.items():
                collected[k].append(v)
    result = {}
    for k, v in collected.items():
        if v:
            dtype = bool if k.endswith("mask") else np.int64 if "rows" in k else np.float64 if k == "anchor_time_s" else np.float32
            result[k] = np.asarray(v, dtype=dtype)
    quality["windows"] = len(collected["history"])
    return result, quality


def export_physical(source, destination):
    root = Path(source["physical_manifest"]).parent
    manifest = read_json(source["physical_manifest"])
    output = []
    for replay in manifest.get("replays", []):
        for meta in replay.get("streams", []):
            path = safe_child(root, meta["csv"])
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                values = np.array([[numeric(r.get(k)) for k in fields] for r in reader], dtype=np.float64)
            filename = f"physical/{source['sha256']}_{meta['stream_id']}.npz"
            np.savez_compressed(destination/filename, values=values, mask=np.isfinite(values))
            output.append({"file": filename, "fields": fields,
                           "unmapped_fields": [k for k in fields if k.startswith(("state_rel_", "input_bool_candidate_"))],
                           "source": {k: meta[k] for k in ("stream_id", "profile", "byte_start", "byte_end", "records", "approx_sample_hz")}})
    return output


def build_dataset(sources, out_dir, config=WindowConfig()):
    config.validate()
    destination = Path(out_dir).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("output directory must be empty; choose a new directory to preserve existing datasets")
    unique = {}
    for source in sources:
        digest = source["sha256"].lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid source SHA256")
        source = dict(source, sha256=digest)
        if digest in unique:
            continue
        summary = read_json(source["telemetry_summary"])
        if str(summary.get("source_sha256", "")).lower() != digest:
            raise ValueError("telemetry/source SHA256 mismatch")
        if not summary.get("telemetry_semantic_signature"):
            raise ValueError("unversioned telemetry cannot be used for training")
        unique[digest] = (source, summary)
    if not unique:
        raise ValueError("no replay sources")
    for name in ("physical", "frames", "sequences"):
        (destination/name).mkdir(parents=True, exist_ok=True)
    splits = split_replays(unique, config.seed)
    entries, physical, replay_info = [], [], []
    sum_x = np.zeros(len(FEATURES)); sum_x2 = sum_x.copy(); counts = sum_x.copy()
    totals = {k: 0 for k in ("train", "val", "test")}
    sample_index = destination/"samples.jsonl"
    with sample_index.open("w", encoding="utf-8") as index:
        for digest, (source, summary) in sorted(unique.items()):
            physical.extend(export_physical(source, destination))
            replay_info.append({"sha256": digest, "split": splits[digest],
                                "profile": summary.get("detected_profile"),
                                "telemetry_signature": summary["telemetry_semantic_signature"]})
            root = Path(source["telemetry_summary"]).parent
            for meta in summary["streams"]:
                stream_id = meta["id"]
                if not stream_id.replace("_", "").isalnum():
                    raise ValueError("invalid stream id")
                data = load_stream(safe_child(root, meta["csv"]), meta)
                data["native_speed"] = meta.get("speed_source") == "replay_linear_velocity"
                name = f"{digest}_{stream_id}"
                frame_file = f"frames/{name}.npz"
                # All unknown values retain their mask. A zero is never substituted without one.
                np.savez_compressed(destination/frame_file,
                    time_s=data["time"], values=data["values"], mask=data["mask"],
                    pose_valid=data["pose_valid"], pose_break_before=data["breaks"],
                    source_stream=data["source_stream"],
                    input_candidates=data["candidates"], input_candidate_mask=data["candidate_mask"],
                    controls=np.full((len(data["time"]), len(CONTROL_NAMES)), -1, dtype=np.int8),
                    control_mask=np.zeros((len(data["time"]), len(CONTROL_NAMES)), dtype=bool),
                    event_time_s=data["event_time"], event_code=data["event_codes"])
                selected = config.include_network or meta.get("role") == "local_high_frequency"
                windows, quality = build_windows(data, config) if selected else ({}, {"windows": 0})
                entry = {"replay_sha256": digest, "stream_id": stream_id, "role": meta.get("role"),
                         "split": splits[digest], "frames_file": frame_file,
                         "frame_count": len(data["time"]), "speed_source": meta.get("speed_source"),
                         "event_table_available": data["event_available"],
                         "event_semantics_available": data["event_semantics"],
                         "control_labels_available": False, "quality": quality,
                         "capabilities": {k: meta.get(k) for k in (
                             "system_drift_state_available", "system_drift_state_status", "speed_effect_state_available",
                             "speed_effect_state_status", "lap_status", "vehicle_forward_axis_local",
                             "native_action_event_ownership", "input_bool_labels_assigned")},
                         "native_evidence": {k: meta.get(k) for k in (
                             "system_drift_segments", "native_speed_effect_segments")}}
                # Upstream legacy summaries may carry a replay-global production block
                # on network shadows. Keep that block only on its authoritative owner.
                if meta.get("role") == "local_high_frequency":
                    entry["native_evidence"]["production_actions"] = meta.get("production_actions")
                if quality["windows"]:
                    filename = f"sequences/{name}.npz"
                    np.savez_compressed(destination/filename, **windows)
                    entry["sequences_file"] = filename
                    totals[splits[digest]] += quality["windows"]
                    for i, t in enumerate(windows["anchor_time_s"]):
                        index.write(json.dumps({"file": filename, "row": i, "split": splits[digest],
                                                "replay_sha256": digest, "stream_id": stream_id,
                                                "anchor_time_s": float(t)})+"\n")
                    if splits[digest] == "train":
                        x = windows["history"].astype(np.float64)
                        m = windows["history_mask"]
                        sum_x += np.where(m, x, 0).sum(axis=(0, 1))
                        sum_x2 += np.where(m, x*x, 0).sum(axis=(0, 1))
                        counts += m.sum(axis=(0, 1))
                entries.append(entry)
    if not sum(totals.values()) or not totals["train"]:
        raise ValueError("no valid training windows; inspect source timing/pose quality and window settings")
    mean = np.divide(sum_x, counts, out=np.zeros_like(sum_x), where=counts > 0)
    variance = np.divide(sum_x2, counts, out=np.zeros_like(sum_x), where=counts > 0)-mean**2
    scale = np.sqrt(np.maximum(variance, 0))
    scale[scale < 1e-6] = 1
    write_json(destination/"normalization.json", {"fit_split": "train", "fields": FEATURES,
                                               "mean": mean.tolist(), "scale": scale.tolist(), "count": counts.astype(int).tolist()})
    manifest = {"contract": CONTRACT, "configuration": asdict(config), "replays": replay_info,
                "state_fields": STATE_FIELDS, "feature_fields": FEATURES,
                "effect_fields": EFFECT_FIELDS, "event_names": EVENT_NAMES, "event_codes": EVENT_CODES,
                "control_names": CONTROL_NAMES, "samples": totals, "streams": entries, "physical_streams": physical,
                "ready": {"trajectory_prediction": True, "keyboard_behavior_cloning": False, "vision": False},
                "units": {"position": "game_coordinate_units", "time": "seconds", "angles": "radians",
                          "velocity": "game_coordinate_units_per_second"},
                "semantics": {"trajectory_frame": "world axes, translated by the last observed history position",
                              "history": "as-of sampling; backwards finite-difference velocity; no future interpolation",
                              "targets": "future positions linearly interpolated within a continuous valid segment",
                              "split": "whole replay SHA256; identical content is deduplicated before splitting",
                              "control_labels": "unavailable; input candidates and active effects are not keyboard labels",
                              "unknown_values": "use masks; missing control=-1; no fabricated reward or race terminal",
                              "physical_candidates": "unmapped_fields have no assigned control semantics"}}
    write_json(destination/"manifest.json", manifest)
    return manifest


class AutodriveDataset:
    """NumPy sequence compatible with torch.utils.data.DataLoader's default collator.

    Returns input/mask/time, future trajectory and independently masked auxiliary
    labels. Explicitly concatenating input_mask into a model's input is recommended.
    """

    def __init__(self, root, split="train", normalize=True):
        self.root = Path(root)
        manifest = read_json(self.root/"manifest.json")
        if manifest.get("contract") != CONTRACT:
            raise ValueError("unsupported dataset contract")
        if split not in ("train", "val", "test"):
            raise ValueError("split must be train, val or test")
        with (self.root/"samples.jsonl").open(encoding="utf-8") as handle:
            self.samples = [row for line in handle if (row := json.loads(line))["split"] == split]
        stats = read_json(self.root/"normalization.json")
        self.mean = np.asarray(stats["mean"], dtype=np.float32)
        self.scale = np.asarray(stats["scale"], dtype=np.float32)
        self.normalize = normalize
        self._path, self._shard = None, None

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, item):
        sample = self.samples[item]
        path = safe_child(self.root, sample["file"])
        if path != self._path:
            with np.load(path, allow_pickle=False) as shard:
                self._shard = {k: shard[k] for k in shard.files}
            self._path = path
        i = sample["row"]
        x = self._shard["history"][i].copy()
        m = self._shard["history_mask"][i]
        if self.normalize:
            x = (x-self.mean)/self.scale
        return {"input": np.where(m, x, 0).astype(np.float32), "input_mask": m.copy(),
                "history_time_s": self._shard["history_time_s"][i].copy(),
                "target_trajectory": self._shard["future_position_delta"][i].copy(),
                "target_effects": self._shard["future_effects"][i].copy(),
                "target_effect_mask": self._shard["future_effect_mask"][i].copy(),
                "target_events": self._shard["future_events"][i].copy(),
                "target_event_mask": self._shard["future_event_mask"][i].copy()}
