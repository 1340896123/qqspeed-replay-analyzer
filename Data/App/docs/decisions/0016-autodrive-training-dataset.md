# 0016 - Autonomous-driving training dataset

Date: 2026-10-23
Status: accepted

## Context

Production telemetry already exposes motion and independent native action authorities, but it is
an analysis interchange format. It has repeated clocks, pose breaks, absent values represented by
legacy false defaults and six unassigned input candidates. Feeding the CSV straight into a policy
would turn unknown effects into negative examples and could confuse effect durations with key holds.

## Decision

Add an optional offline exporter that reuses the existing C# compatibility extractor and telemetry
driver. Keep numerical physical evidence, logical frames and supervised temporal sequences in
separate NPZ files. Publish field order, units, source hashes, native availability, quality counters
and training readiness in a manifest; publish that manifest only after conversion succeeds.

The supported primary learning task is future trajectory prediction. Future native effects and
event markers are optional masked targets. The dataset explicitly has no confirmed keyboard
control labels, images, reward or race-terminal label. Unknown candidates remain separately named
evidence. Only the local authoritative shadow owns replay-global native action events.

Use causal as-of sampling for history. Derive historical velocities backwards within each valid
segment; do not use the analysis driver's central-difference speed/slip as past observations.
Future positions may be interpolated inside valid continuous telemetry. Keep world axes and
translate by the latest historical observation. Units remain game-coordinate units.

Break at invalid pose, backward clock, excessive gaps, physical-part changes and explicit pose
breaks. Retain raw duplicate records, but use their last valid observation per clock for training.
Deduplicate source content before deterministic whole-replay splits; fit normalization on train.

## Consequences and validation

The analyzer UI still needs no Python. The optional tool requires Python 3.10+ and NumPy, provides
a NumPy sequence compatible with PyTorch DataLoader, and includes a ridge-regression training
baseline. Existing outputs are never overwritten. Runtime caches, logs and private samples stay
outside tracked source. No game process or client code is executed.

Synthetic regressions cover missing-authority masks, causality, breaks, clock duplication, action
semantics, split isolation, normalization and a real command invocation over a generated SAV byte
fixture. The optional training-data smoke joins the Fast Gate and explicitly skips if its optional
dependencies are missing. Local private SAVs are separately exported and every sample loaded for
acceptance; private files and machine-local paths are never committed.
