"""Temporal, ownership and byte-provenance contracts; no private game fixtures."""

import csv
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP / "Modules" / "ReplayInputs"))
from replay_inputs import decode_stream, export_sources


def fixture(times=(0, 600, 616, 616, 632), flags=None, omitted=1):
    start, stride = 64, 230
    data = bytearray(start + len(times) * stride + 400)
    struct.pack_into("<I", data, 0, 55)
    struct.pack_into("<I", data, start-4, len(times))
    flags = flags or [(0,) * 8] * len(times)
    for i, (time, values) in enumerate(zip(times, flags)):
        offset = start + i * stride
        struct.pack_into("<I", data, offset, time)
        struct.pack_into("<4f", data, offset+8, 0, 0, 0, 1)
        struct.pack_into("<3f", data, offset+24, i, i/2, 0)
        data[offset+60:offset+68] = bytes(values)
    meta = dict(profile="2026", stream_id="stream_01", byte_start=start+omitted*stride,
                byte_end=start+(len(times)-1)*stride, records=len(times)-omitted, approx_sample_hz=60)
    return data, meta


class ReplayInputContract(unittest.TestCase):
    def setUp(self):
        scratch = APP.parents[1] / ".temp" / "replay-input-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, data, meta):
        path, manifest = self.root/"sample.sav", self.root/"physical.json"
        path.write_bytes(data)
        manifest.write_text(json.dumps(dict(replays=[dict(source_size_bytes=len(data), streams=[meta])])), encoding="utf-8")
        return dict(replay_path=str(path), physical_manifest=str(manifest), sha256=hashlib.sha256(data).hexdigest())

    def test_native_count_restores_initial_frame_with_long_first_gap(self):
        data, meta = fixture()
        frames, _, _, report = decode_stream(data, meta)
        self.assertEqual([row["time_ms"] for row in frames], [0, 600, 616, 616, 632])
        self.assertEqual(report["restored_prefix_frames"], 1)
        self.assertEqual(report["duplicate_clock_rows"], 1)
        self.assertEqual(report["native_count_offset"], 60)

    def test_edges_keep_file_order_and_do_not_invent_boundary_events(self):
        flags = [(1,0,0,0,0,0,0,0), (1,0,1,0,0,0,0,0), (0,0,1,0,0,0,0,1),
                 (0,1,1,0,0,0,0,0), (0,1,1,0,0,1,1,0)]
        data, meta = fixture(flags=flags)
        frames, events, intervals, _ = decode_stream(data, meta)
        self.assertEqual(events[0]["transition"], "initial")
        self.assertEqual(events[0]["control"], "left")
        up = next(event for event in events if event["control"] == "up")
        self.assertEqual((up["previous_time_ms"], up["time_ms"]), (0, 600))
        self.assertEqual([(e["record_index"], e["transition"]) for e in events if e["control"] == "up_secondary"],
                         [(2, "press"), (3, "release")])
        self.assertTrue(any(i["start_censored"] for i in intervals if i["control"] == "left"))
        self.assertTrue(all(i["end_censored"] for i in intervals if i["control"] in ("right", "up", "nitro", "reset")))
        self.assertFalse(any(e["transition"] == "release" and e["control"] == "nitro" for e in events))
        for row in frames:
            # Audit the public, named fields against independent wire positions.
            expected = dict(left=60, right=61, up=62, down=63, drift=64, nitro=65, reset=66, up_secondary=67)
            for name, relative in expected.items():
                self.assertEqual(row[name], data[row["file_offset"] + relative])
        for event in events:
            self.assertEqual(event["state"], data[event["file_offset"]])

    def test_corrupt_layout_clock_or_flags_fail_closed(self):
        for mutation in ("count", "clock", "flag", "version", "profile", "truncated", "end"):
            with self.subTest(mutation=mutation):
                data, meta = fixture()
                if mutation == "count": struct.pack_into("<I", data, 60, 999)
                if mutation == "clock": struct.pack_into("<I", data, 64+3*230, 1)
                if mutation == "flag": data[64+2*230+64] = 2
                if mutation == "version": struct.pack_into("<I", data, 0, 56)
                if mutation == "profile": meta["profile"] = "2021"
                if mutation == "truncated": data = data[:1000]
                if mutation == "end": meta["byte_end"] += 1
                with self.assertRaises(ValueError): decode_stream(data, meta)

    def test_remote_or_ambiguous_ownership_is_not_zero_labels(self):
        data, meta = fixture()
        source = self.source(data, meta)
        for streams in ([dict(meta, approx_sample_hz=15)], [meta, dict(meta, stream_id="stream_02")]):
            Path(source["physical_manifest"]).write_text(json.dumps(dict(replays=[dict(source_size_bytes=len(data), streams=streams)])))
            with self.assertRaises(ValueError): export_sources([source], self.root/"out")
            self.assertFalse((self.root/"out"/"manifest.json").exists())

    def test_export_deduplicates_and_preserves_existing_outputs(self):
        data, meta = fixture()
        source = self.source(data, meta)
        result = export_sources([source, source], self.root/"out")
        self.assertEqual(result["totals"]["replays"], 1)
        self.assertEqual(result["totals"]["frames"], 5)
        before = (self.root/"out"/"manifest.json").read_bytes()
        with self.assertRaises(ValueError): export_sources([source], self.root/"out")
        self.assertEqual((self.root/"out"/"manifest.json").read_bytes(), before)
        source["sha256"] = "0" * 64
        with self.assertRaises(ValueError): export_sources([source], self.root/"changed")
        self.assertFalse((self.root/"changed").exists())

    @unittest.skipUnless(os.name == "nt", "PowerShell/.NET extraction integration runs on Windows")
    def test_sav_to_csv_through_real_physical_detector(self):
        times = [0] + [600+i*16 for i in range(139)]
        flags = [(0,0,int(i>5),0,int(20<=i<30),int(60<=i<65),int(i==90),int(40<=i<50)) for i in range(140)]
        data, _ = fixture(times, flags)
        path = self.root/"sample.sav"
        path.write_bytes(data)
        before = hashlib.sha256(data).hexdigest()
        output = self.root/"integration"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                 str(APP/"Tools"/"ReplayInputs"/"Export-ReplayInputs.ps1"), "-ReplayPath", str(path),
                                 "-OutDir", str(output), "-PythonCommand", sys.executable], capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace")+result.stderr.decode(errors="replace"))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        manifest = json.loads((output/"manifest.json").read_text())
        self.assertEqual(manifest["totals"]["frames"], 140)
        with (output/manifest["replays"][0]["frames_file"]).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([int(row["up_secondary"]) for row in rows], [flag[7] for flag in flags])


if __name__ == "__main__":
    unittest.main(verbosity=2)
