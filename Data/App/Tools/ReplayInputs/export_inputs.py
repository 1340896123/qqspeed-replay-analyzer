"""CLI used by Export-ReplayInputs.ps1; standard library only."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Modules" / "ReplayInputs"))
from replay_inputs import export_sources, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        result = export_sources(read_json(args.sources), args.out)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Input extraction failed: {error}\n")
    print(f"Exported {result['totals']['replays']} SAVs / {result['totals']['frames']} input frames / {result['totals']['events']} observations and transitions")


if __name__ == "__main__":
    main()
