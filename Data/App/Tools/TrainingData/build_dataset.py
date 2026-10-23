"""Convert the extraction index into a self-contained NumPy training dataset."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"Modules"/"TrainingData"))
from replay_dataset import WindowConfig, build_dataset, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--hz", type=float, default=20)
    parser.add_argument("--history", type=int, default=10)
    parser.add_argument("--future", type=int, default=20)
    parser.add_argument("--stride", type=int, default=5)
    parser.add_argument("--max-gap-ms", type=float, default=150)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--include-network", action="store_true")
    args = parser.parse_args()
    config = WindowConfig(args.hz, args.history, args.future, args.stride,
                          args.max_gap_ms/1000, args.seed, args.include_network)
    manifest = build_dataset(read_json(args.sources), args.out, config)
    print(json.dumps({"contract": manifest["contract"], "samples": manifest["samples"],
                      "ready": manifest["ready"]}, indent=2))


if __name__ == "__main__":
    main()
