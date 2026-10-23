"""Fit a small ridge trajectory predictor to verify an exported dataset end to end."""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"Modules"/"TrainingData"))
from replay_dataset import AutodriveDataset, read_json, write_json


def inputs(sample):
    return np.r_[sample["input"].ravel(), sample["input_mask"].astype(np.float32).ravel(),
                 sample["history_time_s"], 1.0].astype(np.float64)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="New model output directory")
    parser.add_argument("--ridge", type=float, default=10.0)
    args = parser.parse_args()
    if not np.isfinite(args.ridge) or args.ridge <= 0:
        parser.error("--ridge must be finite and positive")
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("model output must be empty")
    training = AutodriveDataset(args.dataset, "train")
    if not len(training):
        parser.error("dataset contains no training samples")
    x = np.stack([inputs(training[i]) for i in range(len(training))])
    target_shape = training[0]["target_trajectory"].shape
    y = np.stack([training[i]["target_trajectory"].ravel() for i in range(len(training))])
    penalty = np.eye(x.shape[1])*args.ridge
    penalty[-1, -1] = 0
    weights = np.linalg.solve(x.T@x+penalty, x.T@y)
    metrics = {}
    for split in ("train", "val", "test"):
        dataset = AutodriveDataset(args.dataset, split)
        errors = []
        for i in range(len(dataset)):
            sample = dataset[i]
            prediction = (inputs(sample)@weights).reshape(target_shape)
            errors.extend(np.linalg.norm(prediction-sample["target_trajectory"], axis=-1).tolist())
        metrics[split] = {"samples": len(dataset), "mean_position_error_game_units":
                          float(np.mean(errors)) if errors else None}
    args.out.mkdir(parents=True, exist_ok=True)
    stats = read_json(args.dataset/"normalization.json")
    np.savez_compressed(args.out/"trajectory_ridge.npz", weights=weights,
                        mean=np.array(stats["mean"]), scale=np.array(stats["scale"]),
                        target_shape=np.array(target_shape))
    write_json(args.out/"metrics.json", {"model": "ridge_trajectory_baseline", "ridge": args.ridge,
               "position_units": "game_coordinate_units", "metrics": metrics,
               "limitations": "Offline trajectory prediction only; no keyboard controller or game execution."})
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
