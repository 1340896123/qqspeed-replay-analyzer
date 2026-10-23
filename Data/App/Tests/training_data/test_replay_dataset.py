"""Safety and learning-contract regressions, independent of private replay fixtures."""

import csv
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP/"Modules"/"TrainingData"))
from replay_dataset import (AutodriveDataset, FEATURES, STATE_FIELDS, WindowConfig,
                            build_dataset, build_windows, load_stream, read_json,
                            safe_child, split_replays, write_json)


class DatasetContract(unittest.TestCase):
    def setUp(self):
        scratch = APP.parents[1]/".temp"/"training-data-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(scratch.resolve())
        self.config = WindowConfig(hz=10, history=4, future=3, stride=1, max_gap_s=.15)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name="a", translation=0, effect_available=False):
        directory = self.root/name
        directory.mkdir()
        digest = hashlib.sha256(name.encode()).hexdigest()
        meta = dict(id="shadow_local", role="local_high_frequency", csv="logical_01.csv",
                    speed_source="derived_position_velocity", system_drift_state_available=effect_available,
                    speed_effect_state_available=effect_available, native_action_event_available=effect_available,
                    native_speed_effect_timeline_validation={"nitro_semantic_valid": effect_available},
                    production_actions={"game_facing_available": effect_available},
                    native_action_event_timeline=[{"time_ms": 700, "action_code": 8}] if effect_available else [])
        fields = ["time_s", *STATE_FIELDS, "pose_valid", "pose_break_before", "source_stream",
                  *(f"input_bool_candidate_{i}" for i in range(60, 66))]
        rows = []
        for i in range(40):
            row = {k: "" for k in fields}
            row.update(time_s=i/10, x=translation+i/10, y=2*i/10, z=0, yaw=0,
                       speed=9999, speed_derived=9999, speed_raw=9999, pose_valid="True",
                       pose_break_before="False", source_stream="stream_01")
            row.update({k: "False" for k in STATE_FIELDS[14:]})
            row["input_bool_candidate_60"] = i % 2
            rows.append(row)
        with (directory/meta["csv"]).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader(); writer.writerows(rows)
        summary = {"source_sha256": digest, "telemetry_semantic_signature": "test-contract",
                   "detected_profile": "2021", "streams": [meta]}
        write_json(directory/"telemetry_summary.json", summary)
        raw = directory/"raw"
        raw.mkdir()
        with (raw/"stream_01.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "qx", "x", "input_bool_candidate_60", "state_rel_+40"])
            writer.writerows([[i/10, 0, i/10, i % 2, 1] for i in range(40)])
        write_json(raw/"manifest.json", {"replays": [{"streams": [{
            "csv": "stream_01.csv", "stream_id": "stream_01", "profile": "2021",
            "byte_start": 0, "byte_end": 5640, "records": 40, "approx_sample_hz": 10}]}]})
        return {"sha256": digest, "telemetry_summary": str(directory/"telemetry_summary.json"),
                "physical_manifest": str(raw/"manifest.json")}, meta

    def data(self, **kwargs):
        source, meta = self.source(**kwargs)
        return load_stream(Path(source["telemetry_summary"]).parent/meta["csv"], meta)

    def test_absent_authority_masks_false_defaults_and_candidate_controls(self):
        data = self.data()
        for name in STATE_FIELDS[14:]:
            self.assertFalse(data["mask"][:, STATE_FIELDS.index(name)].any())
        self.assertTrue(data["candidate_mask"][:, 0].all())
        windows, _ = build_windows(data, self.config)
        self.assertFalse(windows["future_effect_mask"].any())
        self.assertFalse(windows["future_event_mask"].any())

    def test_history_is_causal_even_when_future_and_central_speed_change(self):
        data = self.data()
        before, _ = build_windows(data, self.config)
        anchor = before["anchor_time_s"][0]
        later = data["time"] > anchor+1e-8
        data["values"][later, 0] += 50
        data["values"][:, STATE_FIELDS.index("speed")] *= 100
        data["values"][:, STATE_FIELDS.index("speed_derived")] *= 100
        after, _ = build_windows(data, self.config)
        np.testing.assert_array_equal(before["history"][0], after["history"][0])
        self.assertFalse(np.array_equal(before["future_position_delta"][0], after["future_position_delta"][0]))
        np.testing.assert_allclose(before["history"][0, 1:, FEATURES.index("speed")], np.sqrt(5), rtol=1e-5)

    def test_interpolation_recovers_known_linear_trajectory(self):
        data = self.data()
        windows, _ = build_windows(data, self.config)
        expected = np.array([[.1, .2, 0], [.2, .4, 0], [.3, .6, 0]])
        np.testing.assert_allclose(windows["future_position_delta"][0], expected, atol=1e-6)
        self.assertTrue(np.all(windows["history_time_s"] <= 1e-8))

    def test_no_window_crosses_gap_invalid_pose_or_physical_stream(self):
        for mode in ("gap", "pose", "stream", "break", "clock"):
            with self.subTest(mode=mode):
                # Reuse one source without creating a duplicate fixture directory.
                if not (self.root/"a").exists():
                    source, meta = self.source()
                data = load_stream(self.root/"a"/"logical_01.csv", meta)
                if mode == "gap": data["time"][20:] += 1
                elif mode == "pose": data["pose_valid"][20] = False
                elif mode == "stream": data["source_stream"][20:] = "stream_02"
                elif mode == "break": data["breaks"][20] = True
                else: data["time"][20:] -= 2
                windows, _ = build_windows(data, self.config)
                for hi, right in zip(windows["history_rows"], windows["future_rows_right"]):
                    self.assertFalse(hi[0] < 20 <= right[-1])

    def test_duplicate_clock_keeps_latest_without_zero_division(self):
        data = self.data()
        data["time"][10] = data["time"][9]
        config = WindowConfig(hz=10, history=4, future=3, max_gap_s=.25)
        windows, quality = build_windows(data, config)
        self.assertEqual(quality["duplicate_clock_rows"], 1)
        self.assertTrue(np.isfinite(windows["history"]).all())
        self.assertNotIn(9, windows["history_rows"])

    def test_authoritative_events_are_labels_not_keyboard_holds(self):
        data = self.data(effect_available=True)
        windows, _ = build_windows(data, self.config)
        self.assertTrue(windows["future_event_mask"].all())
        self.assertTrue(windows["future_events"].any())
        self.assertTrue(windows["future_effect_mask"].all())

    def test_replay_split_deduplication_training_statistics_and_loader(self):
        hashes = [hashlib.sha256(c.encode()).hexdigest() for c in "abc"]
        split = split_replays(hashes, 42)
        sources = []
        for c, digest in zip("abc", hashes):
            source, _ = self.source(c, translation=0 if split[digest] == "train" else 1e6)
            sources.append(source)
        out = self.root/"dataset"
        manifest = build_dataset([*sources, sources[0]], out, self.config)
        self.assertEqual(len(manifest["replays"]), 3)
        self.assertTrue(all(manifest["samples"][s] > 0 for s in ("train", "val", "test")))
        memberships = {}
        for stream in manifest["streams"]:
            memberships.setdefault(stream["replay_sha256"], set()).add(stream["split"])
        self.assertTrue(all(len(v) == 1 for v in memberships.values()))
        dataset = AutodriveDataset(out)
        sample = dataset[0]
        self.assertEqual(sample["input"].shape, (self.config.history, len(FEATURES)))
        self.assertTrue(np.isfinite(sample["input"]).all())
        self.assertTrue(np.all(sample["input"][~sample["input_mask"]] == 0))
        stats = read_json(out/"normalization.json")
        train_stream = next(s for s in manifest["streams"] if s["split"] == "train")
        with np.load(out/train_stream["sequences_file"], allow_pickle=False) as shard:
            x, m = shard["history"], shard["history_mask"]
            for k in range(len(FEATURES)):
                if m[:, :, k].any():
                    self.assertAlmostEqual(stats["mean"][k], float(x[:, :, k][m[:, :, k]].mean(dtype=np.float64)), places=5)
        for stream in manifest["streams"]:
            with np.load(out/stream["frames_file"], allow_pickle=False) as frames:
                self.assertFalse(frames["control_mask"].any())
                self.assertTrue(np.all(frames["controls"] == -1))
        self.assertFalse(manifest["ready"]["keyboard_behavior_cloning"])
        self.assertFalse(any(str(self.root) in f.read_text(encoding="utf-8") for f in out.glob("*.json")))

    def test_validation_rejects_unversioned_sources_and_existing_output(self):
        source, _ = self.source()
        out = self.root/"existing"
        out.mkdir(); (out/"keep.txt").write_text("keep")
        with self.assertRaises(ValueError): build_dataset([source], out, self.config)
        self.assertEqual((out/"keep.txt").read_text(), "keep")
        summary = read_json(source["telemetry_summary"])
        summary["source_sha256"] = "bad"
        write_json(source["telemetry_summary"], summary)
        with self.assertRaisesRegex(ValueError, "SHA256"): build_dataset([source], self.root/"bad", self.config)
        with self.assertRaises(ValueError): safe_child(self.root, "../escape.csv")
        for kwargs in ({"hz": float("nan")}, {"max_gap_s": float("inf")}, {"history": 0}):
            with self.assertRaises(ValueError): WindowConfig(**kwargs).validate()

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows extraction integration")
    def test_actual_sav_command_exports_loadable_sequences_without_invented_actions(self):
        stride, count = 230, 240
        raw = bytearray(stride*count+512)
        for i in range(count):
            offset = i*stride
            struct.pack_into("<I", raw, offset, i*17)
            struct.pack_into("<4f", raw, offset+8, 0, 0, 0, 1)
            struct.pack_into("<3f", raw, offset+24, i*.2, np.sin(i/30)*2, 0)
            struct.pack_into("<i", raw, offset+52, 5)
            raw[offset+60:offset+66] = bytes([i % 2]*6)
            struct.pack_into("<i", raw, offset+76, 1)
            struct.pack_into("<3f", raw, offset+173, .2/.017, 0, 0)
        sav = self.root/"fixture.sav"
        sav.write_bytes(raw)
        output = self.root/"exported"
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                   str(APP/"Tools/TrainingData/Export-AutodriveDataset.ps1"), "-ReplayPath", str(sav),
                   "-OutDir", str(output), "-PythonCommand", sys.executable]
        # A Python process started by PowerShell 7 can inherit incompatible PS7
        # modules. Let Windows PowerShell construct its own native module path.
        environment = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
        process = subprocess.run(command, capture_output=True, timeout=120, env=environment)
        self.assertEqual(process.returncode, 0, process.stdout.decode(errors="replace")+process.stderr.decode(errors="replace"))
        manifest = read_json(output/"manifest.json")
        self.assertGreater(manifest["samples"]["train"], 0)
        sample = AutodriveDataset(output)[0]
        self.assertTrue(np.isfinite(sample["input"]).all())
        self.assertFalse(sample["target_effect_mask"].any())
        self.assertFalse(sample["target_event_mask"].any())
        self.assertIn("qx", manifest["physical_streams"][0]["fields"])
        self.assertIn("file_vx", manifest["physical_streams"][0]["fields"])
        preserved = subprocess.run(command, capture_output=True, timeout=30, env=environment)
        self.assertNotEqual(preserved.returncode, 0)
        self.assertEqual(read_json(output/"manifest.json"), manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
