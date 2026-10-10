param(
    [string]$AppDir = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)
$ErrorActionPreference='Stop'
try { [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false) } catch {}

# ===========================================================================
# Replay time semantics contract (replay_time_semantics_v1) - Fast Gate.
#
# Product defect this closed (audit: Output/Handoff/REPLAY_TIME_AUDIT.md): a recording's raw frame
# span was presented as race time. For 彩虹风车岛-20260925-005647 (SHA256 1986BF76...C89D752) the
# span was 105.527 s against a game result of 85.514 s, because the recording opens with a 9.122 s
# parked countdown and closes with 7.583 s of post-finish footage - and the opening lap was charged
# for the countdown, which also corrupts A/B lap deltas between replays with different parked heads.
#
# THE CONTRACT UNDER TEST:
#   * the raw timeline (time_ms / lap_index) is never modified or rescaled;
#   * recording span and the movement-derived estimate are separate, differently-named quantities;
#   * the estimate is ALWAYS marked as an estimate, and no official race time is ever invented;
#   * only recording-EDGE idle is excluded - a stop inside the movement window stays in;
#   * a lap-time DELTA requires a trustworthy estimate on BOTH sides, else unavailable (fail closed).
#
# All cases are synthetic; the real replay is used only as a values check when its cache is present.
# ===========================================================================

. (Join-Path $AppDir 'Modules\Telemetry\Telemetry.Analysis.ps1')

$checks=0
function Require([bool]$Ok,[string]$Message){ $script:checks++; if(-not $Ok){ throw ('FAILED: '+$Message) } }

function New-TimeRow([double]$T,[double]$Speed,[bool]$PoseValid=$true){
    return [pscustomobject]@{time_s=$T;speed=$Speed;pose_valid=$PoseValid;x=0.0;y=0.0;distance=0.0;lap_index=1}
}

# --- 1. movement window: parked head and tail are identified -------------------------------------
# 5 s parked, 10 s moving, 4 s parked; 100 Hz.
$rows=New-Object System.Collections.Generic.List[object]
for($i=0;$i -lt 500;$i++){ $rows.Add((New-TimeRow (0.0+$i*0.01) 0.0)) }
for($i=0;$i -lt 1000;$i++){ $rows.Add((New-TimeRow (5.0+$i*0.01) 12.0)) }
for($i=0;$i -lt 400;$i++){ $rows.Add((New-TimeRow (15.0+$i*0.01) 0.0)) }
$w=Get-QQReplayMovementWindow -Rows $rows.ToArray()
Require ([bool]$w.available) 'a recording with movement must expose a movement window'
Require ([Math]::Abs([double]$w.start_t-5.0)-lt0.02) ('window start must be the first moving frame, got '+[string]$w.start_t)
Require ([Math]::Abs([double]$w.end_t-14.99)-lt0.02) ('window end must be the last moving frame, got '+[string]$w.end_t)
Require ([Math]::Abs([double]$w.head_idle_s-5.0)-lt0.02) ('head idle must be measured, got '+[string]$w.head_idle_s)
Require ([Math]::Abs([double]$w.tail_idle_s-4.01)-lt0.03) ('tail idle must be measured, got '+[string]$w.tail_idle_s)
Require ([Math]::Abs([double]$w.duration_s-9.99)-lt0.03) ('movement window duration must be the moving span, got '+[string]$w.duration_s)

# --- 2. a completely parked recording has NO movement window (fail closed) ------------------------
$parked=New-Object System.Collections.Generic.List[object]
for($i=0;$i -lt 200;$i++){ $parked.Add((New-TimeRow (0.0+$i*0.01) 0.0)) }
$wParked=Get-QQReplayMovementWindow -Rows $parked.ToArray()
Require (-not[bool]$wParked.available) 'an all-parked recording must not report a movement window'
Require ([string]$wParked.reason -eq 'no_movement_evidence') ('parked reason must be explicit, got '+[string]$wParked.reason)

# --- 3. a stop INSIDE the movement window is never removed ----------------------------------------
# 2 s parked, 4 s moving, 3 s stopped mid-race, 4 s moving, 2 s parked.
$mid=New-Object System.Collections.Generic.List[object]
for($i=0;$i -lt 200;$i++){ $mid.Add((New-TimeRow (0.0+$i*0.01) 0.0)) }
for($i=0;$i -lt 400;$i++){ $mid.Add((New-TimeRow (2.0+$i*0.01) 10.0)) }
for($i=0;$i -lt 300;$i++){ $mid.Add((New-TimeRow (6.0+$i*0.01) 0.0)) }   # in-race stop
for($i=0;$i -lt 400;$i++){ $mid.Add((New-TimeRow (9.0+$i*0.01) 10.0)) }
for($i=0;$i -lt 200;$i++){ $mid.Add((New-TimeRow (13.0+$i*0.01) 0.0)) }
$wMid=Get-QQReplayMovementWindow -Rows $mid.ToArray()
Require ([bool]$wMid.available) 'a mid-race stop must not destroy the movement window'
Require ([Math]::Abs([double]$wMid.duration_s-11.0)-lt0.05) ('the in-race stop must stay INSIDE the window (11.0 s), got '+[string]$wMid.duration_s)
Require ([Math]::Abs([double]$wMid.head_idle_s-2.0)-lt0.02) ('only the head may be excluded, got '+[string]$wMid.head_idle_s)
Require ([Math]::Abs([double]$wMid.tail_idle_s-2.0)-lt0.03) ('only the tail may be excluded, got '+[string]$wMid.tail_idle_s)

# --- 4. lap semantics: edge idle is excluded, the lap value is an ESTIMATE ------------------------
$lap1=Get-QQReplayLapTimeSemantics -SpanStartT 0.0 -SpanEndT 12.0 -Window $w -BoundaryAffected $true
Require ([string]$lap1.contract -eq 'replay_time_semantics_v1') 'lap semantics must carry the contract name'
Require ([bool]$lap1.race_time_estimated) 'a lap overlapping the movement window must yield an estimate'
Require ([Math]::Abs([double]$lap1.race_time_s_estimate-7.0)-lt0.05) ('edge idle must be excluded (7.0 s expected), got '+[string]$lap1.race_time_s_estimate)
Require ([Math]::Abs([double]$lap1.lap_span_s-12.0)-lt0.001) 'the raw span must stay published unchanged'
Require ([string]$lap1.race_time_estimated_status -eq 'estimated_from_movement') 'the estimate status must be explicit'

$lapMid=Get-QQReplayLapTimeSemantics -SpanStartT 6.0 -SpanEndT 13.0 -Window $w -BoundaryAffected $false
Require ([bool]$lapMid.race_time_estimated) 'a mid-recording lap must still be estimable'
Require ([Math]::Abs([double]$lapMid.race_time_s_estimate-7.0)-lt0.05) ('a mid lap is not charged edge idle, got '+[string]$lapMid.race_time_s_estimate)
Require ([Math]::Abs([double]$lapMid.excludes_edge_idle_s-0.0)-lt0.001) 'a mid lap must not report excluded edge idle'

# --- 5. no movement window -> unavailable, never a fabricated number ------------------------------
$lapNoWindow=Get-QQReplayLapTimeSemantics -SpanStartT 20.0 -SpanEndT 73.0 -Window $wParked -BoundaryAffected $true
Require ($null-eq$lapNoWindow.race_time_s_estimate) 'without a movement window the estimate must be null'
Require (-not[bool]$lapNoWindow.race_time_estimated) 'without a movement window the estimate must be flagged unavailable'
Require ([string]$lapNoWindow.race_time_estimated_status -eq 'unavailable') 'the unavailable status must be explicit'

# A lap entirely outside the movement window must not borrow the window's duration.
$lapOutside=Get-QQReplayLapTimeSemantics -SpanStartT 0.0 -SpanEndT 4.0 -Window $w -BoundaryAffected $true
Require (-not[bool]$lapOutside.race_time_estimated) 'a fully parked lap must not be given a race time'
Require ([string]$lapOutside.reason -eq 'lap_outside_movement_window') ('the outside-window reason must be explicit, got '+[string]$lapOutside.reason)

# --- 6. an implausible window fails closed --------------------------------------------------------
$tinyWindow=[pscustomobject]@{available=$true;start_t=10.0;end_t=10.02;duration_s=0.02;head_idle_s=10.0;tail_idle_s=20.0}
$lapTiny=Get-QQReplayLapTimeSemantics -SpanStartT 0.0 -SpanEndT 30.0 -Window $tinyWindow -BoundaryAffected $true
Require (-not[bool]$lapTiny.race_time_estimated) 'a 0.02 s movement window inside a 30 s recording must fail closed'
Require ([string]$lapTiny.reason -eq 'movement_window_implausibly_short') ('the implausible reason must be explicit, got '+[string]$lapTiny.reason)

# --- 7. A/B safety: differing parked heads must not manufacture a lap delta -----------------------
# Two recordings with IDENTICAL driving (same 10 s of movement) but different parked heads.
function New-RecordingWithHead([double]$HeadS){
    $l=New-Object System.Collections.Generic.List[object]
    $n=[int]($HeadS*100)
    for($i=0;$i -lt $n;$i++){ $l.Add((New-TimeRow ($i*0.01) 0.0)) }
    for($i=0;$i -lt 1000;$i++){ $l.Add((New-TimeRow ($HeadS+$i*0.01) 12.0)) }
    for($i=0;$i -lt 300;$i++){ $l.Add((New-TimeRow ($HeadS+10.0+$i*0.01) 0.0)) }
    return $l.ToArray()
}
$recA=New-RecordingWithHead 2.0
$recB=New-RecordingWithHead 30.0
$wA=Get-QQReplayMovementWindow -Rows $recA
$wB=Get-QQReplayMovementWindow -Rows $recB
$semA=Get-QQReplayLapTimeSemantics -SpanStartT ([double]$recA[0].time_s) -SpanEndT ([double]$recA[$recA.Count-1].time_s) -Window $wA -BoundaryAffected $true
$semB=Get-QQReplayLapTimeSemantics -SpanStartT ([double]$recB[0].time_s) -SpanEndT ([double]$recB[$recB.Count-1].time_s) -Window $wB -BoundaryAffected $true
# Raw spans differ by 28 s (2 vs 30 s of parked head) - that is exactly the trap.
$spanA=[Math]::Round(([double]$recA[$recA.Count-1].time_s-[double]$recA[0].time_s),4)
$spanB=[Math]::Round(([double]$recB[$recB.Count-1].time_s-[double]$recB[0].time_s),4)
Require ([Math]::Abs($spanA-$spanB)-gt20.0) 'the raw spans must differ in this scenario (otherwise the test proves nothing)'
Require ([Math]::Abs([double]$semA.race_time_s_estimate-[double]$semB.race_time_s_estimate)-lt0.05) `
    ('estimates must match for identical driving, got '+[string]$semA.race_time_s_estimate+' vs '+[string]$semB.race_time_s_estimate)
# The raw-span delta would be a false 28 s; the estimate delta is ~0.
$falseDelta=[Math]::Round($spanA-$spanB,4)
$trueDelta=[Math]::Round(([double]$semA.race_time_s_estimate-[double]$semB.race_time_s_estimate),4)
Require ([Math]::Abs($trueDelta)-lt0.05) ('the comparable delta must be ~0, got '+[string]$trueDelta)
Require ([Math]::Abs($falseDelta)-gt20.0) ('the raw-span delta must be the false one being avoided, got '+[string]$falseDelta)

# --- 8. the native timeline is reported as unchanged ----------------------------------------------
$lapUnchanged=Get-QQReplayLapTimeSemantics -SpanStartT 20.0 -SpanEndT 72.976 -Window $w -BoundaryAffected $true
Require ([Math]::Abs([double]$lapUnchanged.lap_span_s-52.976)-lt0.001) ('the raw lap span must be preserved exactly, got '+[string]$lapUnchanged.lap_span_s)

# --- 9. real replay values check (only when its cache is present) ---------------------------------
$dataDir=Split-Path -Parent $AppDir
$csv=Join-Path $dataDir 'Telemetry\1986BF76316948B2\logical_01.csv'
if(Test-Path -LiteralPath $csv){
    $lines=[IO.File]::ReadAllLines($csv)
    $hdr=$lines[0].Split(',')
    $iT=[array]::IndexOf($hdr,'time_s');$iS=[array]::IndexOf($hdr,'speed');$iP=[array]::IndexOf($hdr,'pose_valid')
    $real=New-Object System.Collections.Generic.List[object]
    for($i=1;$i -lt $lines.Count;$i++){
        $c=$lines[$i].Split(',')
        $pv=($c[$iP] -eq 'True')
        $real.Add([pscustomobject]@{time_s=[double]$c[$iT];speed=[double]$c[$iS];pose_valid=$pv})
    }
    $arr=$real.ToArray()
    $span=[Math]::Round(([double]$arr[$arr.Count-1].time_s-[double]$arr[0].time_s),4)
    Require ([Math]::Abs($span-105.527)-lt0.001) ('the real replay span must stay 105.527 s, got '+[string]$span)
    $rw=Get-QQReplayMovementWindow -Rows $arr
    Require ([bool]$rw.available) 'the real replay must expose a movement window'
    # The audit quoted the boundary at the first >=1.0 m/s frame; the detector uses 0.5 m/s, which
    # legitimately shifts head/tail by <0.25 s. So these assert STRUCTURE (sign, magnitude, ordering,
    # decomposition) rather than re-typing the audit's constants, which would only test the threshold.
    Require ([double]$rw.head_idle_s-gt5.0) ('the real recording must expose a substantial parked head, got '+[string]$rw.head_idle_s)
    Require ([double]$rw.tail_idle_s-gt5.0) ('the real recording must expose a substantial parked tail, got '+[string]$rw.tail_idle_s)
    Require ([double]$rw.duration_s-gt70.0) ('the real movement window must dominate the span, got '+[string]$rw.duration_s)
    Require ([double]$rw.duration_s-lt$span) 'the movement window must be shorter than the recording span'
    Require ([double]$rw.head_idle_s-lt[double]$rw.duration_s) 'the parked head must be far shorter than the moving window'
    # Invariant: head + movement + tail must reproduce the recorded span exactly, whatever threshold
    # is used. This is what makes the decomposition trustworthy rather than three loose numbers.
    $recomposed=[Math]::Round([double]$rw.head_idle_s+[double]$rw.duration_s+[double]$rw.tail_idle_s,3)
    Require ([Math]::Abs($recomposed-$span)-lt0.01) ('head+movement+tail must equal the span ('+[string]$span+'), got '+[string]$recomposed)
    # The estimate must NOT be the 85.514 s game result: guessing an official time is forbidden.
    Require ([Math]::Abs([double]$rw.duration_s-85.514)-gt3.0) 'the movement estimate must not be presented as the official 85.514 s result'
    # Exact identity: the published decomposition must reproduce the span to the millisecond, and the
    # window must be exactly end - start under the inclusive-boundary rule. This is what makes the
    # three numbers a real decomposition rather than three independently reported quantities.
    $h=[double]$rw.head_idle_s; $wnd=[double]$rw.duration_s; $tl=[double]$rw.tail_idle_s
    Require ([Math]::Abs(($h+$wnd+$tl)-$span)-lt0.001) ('head+window+tail must equal the span exactly, got '+[string]($h+$wnd+$tl)+' vs '+[string]$span)
    Require ([Math]::Abs(($wnd-([double]$rw.end_t-[double]$rw.start_t)))-lt0.0005) 'the window must be exactly end_t - start_t (inclusive boundary rule)'
    Require ([Math]::Abs(($h-([double]$rw.start_t-[double]$arr[0].time_s)))-lt0.0005) 'head must be exactly start_t - first_frame'
    Require ([Math]::Abs(($tl-([double]$arr[$arr.Count-1].time_s-[double]$rw.end_t)))-lt0.0005) 'tail must be exactly last_frame - end_t'
    # No replay-specific constant may exist: the boundary is the threshold crossing, not a magic offset.
    $impl=[IO.File]::ReadAllText((Join-Path $AppDir 'Modules\Telemetry\Telemetry.Analysis.ps1'))
    Require (-not($impl -match '9\.072|9\.122|7\.383|7\.583|89\.072|88\.822|105\.527')) 'the detector must contain no replay-specific duration constant'
    Write-Host ('  [real] span='+[string]$span+' head='+[string]$h+' window='+[string]$wnd+' tail='+[string]$tl+' official 85.514 is NOT claimed')
} else {
    Write-Host '  [real] replay cache absent -> value checks skipped (synthetic contract still enforced)'
}

Write-Host ''
Write-Host ('[OK] Replay time semantics contract passed. checks='+$checks+' contract=replay_time_semantics_v1 span!=race-time edge-idle-excluded in-race-stops-preserved ab-delta-gated official-time=never-invented')
exit 0
