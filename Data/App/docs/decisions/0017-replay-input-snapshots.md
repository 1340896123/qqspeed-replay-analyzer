# ADR 0017 — SAV playback input snapshots

Date: 2026-10-23. Status: accepted for the standalone input exporter.

## Evidence

Static analysis of the 64-bit `Top-Kart.dll` identifies the complete native path. Addresses below are preferred-image virtual addresses, with image base `0x180000000`. Function descriptions are analysis labels, not recovered C++ symbol names. No client code is executed.

1. `0x180180A70` opens a `.sav` for reading and loads its header and record data. `0x18017F4C0` allocates 240-byte MoveInfo objects and calls `0x1801A10A0` to deserialize each object.
2. `0x1801A10A0` reads thirteen 4-byte values, then, for container version >=9, two 4-byte values followed by eight separate 1-byte values into MoveInfo members 68..75. Their serialized file positions are therefore 60..67. Later version-dependent additions change record length, not this prefix. Versions 50..55 with the extended block have a serialized stride of 230 bytes. The existing detector verifies that stride and vehicle geometry.
3. `0x18017E2C0` selects records for the native playback clock. It copies those eight flags from the selected preceding record. It interpolates pose independently; input flags are discrete snapshots.
4. `0x180E944F0` applies the selected MoveInfo and calls `0x180E94CE0`, which assigns the flags to playback input-state members in the order below and updates the corresponding HUD states.
5. `0x180A2D680` establishes input-state members 0/1/2/3/4/5 as Up/Down/Right/Left/Shift/Ctrl by reading logical key codes 121/126/124/123/42/29. The key-name table at `0x1828D06E0` names those codes. `0x180CF5D30` records reset from code 19 (R), and separately records the primary and secondary bindings of code 121 in input-state members 0 and 7. The secondary binding is not a forward-release event. The separate JSON race recorder at `0x180EEC260` computes a forward-release edge itself; that JSON field must not be substituted for SAV byte 67.

| File byte | MoveInfo member | Playback input-state member | Meaning |
| --- | --- | --- | --- |
| 60 | 68 | 3 | left |
| 61 | 69 | 2 | right |
| 62 | 70 | 0 | primary forward |
| 63 | 71 | 1 | back/brake |
| 64 | 72 | 4 | drift |
| 65 | 73 | 5 | nitro |
| 66 | 74 | 6 | reset |
| 67 | 75 | 7 | secondary forward |

The distinction between input and successful action/effect remains essential: a nitro press does not prove a nitro activation, and a drift input is not the native system-drift interval.

## Decision

Add an independent command-line exporter using the existing C# physical detector to locate the stream and a Python standard-library module to decode the verified prefix. Require a native MoveInfo count whose end agrees exactly with the detected stream end. Restore up to 32 preceding records when that count proves the detector omitted an initial portion. Preserve all native timestamps and duplicate-time records in file order.

Emit snapshots, observed state changes and censored held intervals with byte provenance. An initial held key is an observation, not a newly inferred press. An interval still held at the last record is censored, not a newly inferred release. Actual edge times are only bounded by consecutive sampling timestamps.

Initially accept a single high-frequency vehicle stream only. Remote input completeness and ownership in multiple-shadow recordings need separate evidence; all-zero remote bytes do not establish released keys. Reject unsupported versions, ambiguous boundaries, malformed booleans and decreasing clocks before publishing a manifest.

## Validation and scope

Synthetic tests cover wire provenance, count-framed prefix restoration, duplicate clocks, initial/end censoring, corrupt records, source hash changes, ownership rejection, deduplication and the actual SAV → C# detector → named CSV command. Private version-55 recordings are verified locally against every exported byte. Client binaries, IDA databases, decompiled output, recordings and generated CSV files stay outside tracked source under the local work directory.

This adds a new, explicit input contract. It does not silently reinterpret the existing trajectory dataset's candidate fields or invalidate its masks. Training integration can consume this export after matching source hashes and timestamps.
