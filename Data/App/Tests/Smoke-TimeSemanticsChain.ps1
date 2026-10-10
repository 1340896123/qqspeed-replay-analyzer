param(
    [string]$AppDir = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)
$ErrorActionPreference='Stop'
try { [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false) } catch {}

# ===========================================================================
# Time-semantics DATA CHAIN contract (Fast Gate, synthetic - no replay needed).
#
# The defect this closes: the per-lap movement estimate was computed correctly but never reached the
# UI. Two independent breaks, both silent:
#   1. `NF-StreamMeta` (Modules/Native/NativeAnalysis.ps1) built the analysis-JSON stream view without
#      `laps` / `time_semantics`, so the analysis JSON carried a stream with no lap list at all.
#   2. The frontend lap record arrives in TWO shapes: the telemetry summary carries
#      `lap.time_semantics.race_time_s_estimate`, while the training lap metrics carry flat
#      `lap_time_estimate_s` + `lap_time_is_estimate`. The JS only understood the flat shape.
# Result: the lap selector could only ever render the raw span, even when an estimate existed.
#
# This test walks the REAL chain: telemetry summary -> NF-StreamMeta -> Convert-StreamMeta, and
# asserts the estimate survives to the frontend payload, in the telemetry (nested) shape.
# ===========================================================================

. (Join-Path $AppDir 'Modules\Native\NativeAnalysis.ps1')
. (Join-Path $AppDir 'Modules\Frontend\Frontend.Backend.ps1')

$checks=0
function Require([bool]$Ok,[string]$Message){ $script:checks++; if(-not $Ok){ throw ('FAILED: '+$Message) } }

# A telemetry summary stream shaped exactly like the real one: the lap record's estimate lives INSIDE
# `time_semantics`, and the stream carries its own `time_semantics` block.
function New-SummaryStream([int]$LapCount=2,[double]$HeadIdle=9.072){
    $laps=@()
    for($i=1;$i -le $LapCount;$i++){
        $span=52.976
        $est=$span-$HeadIdle
        $laps += [pscustomobject]@{
            lap=$i
            start_i=(($i-1)*3175)
            end_i=($i*3175-1)
            start_t=(20.0+(($i-1)*52.976))
            end_t=(20.0+($i*52.976))
            duration_s=$span
            start_distance=0.0
            end_distance=702.688
            distance=702.688
            source='replay_native_lap_index'
            time_semantics=[pscustomobject]@{
                contract='replay_time_semantics_v1'
                lap_span_s=$span
                boundary_affects_time=$true
                race_time_s_estimate=$est
                race_time_estimated=$true
                race_time_estimated_status='estimated_from_movement'
                excludes_edge_idle_s=$HeadIdle
                reason=''
            }
        }
    }
    return [pscustomobject]@{
        id='shadow_local';role='local_high_frequency';role_label='local';sample_hz=58.824;records=6327
        duration_s=105.527;distance=1430.129;avg_speed=13.552;speed_source='replay_linear_velocity'
        lap_count=$LapCount;laps=$laps
        time_semantics=[pscustomobject]@{
            contract='replay_time_semantics_v1'
            recording_duration_s=105.527
            recording_duration_semantics='raw frame span'
            movement_window=[pscustomobject]@{available=$true;start_t=29.072;end_t=118.144;duration_s=89.072;head_idle_s=9.072;tail_idle_s=7.383;speed_threshold_mps=0.5;reason='movement_window_from_speed_threshold'}
            movement_window_duration_s=89.072
            movement_window_status='estimated_from_movement_evidence'
            official_race_duration_s=$null
            official_race_duration_status='unavailable_no_official_timing_source'
        }
        system_drift_state_available=$false;system_drift_segment_count=0
        speed_effect_state_available=$false;native_speed_effect_segment_count=0
        map_propulsion_effect_segment_count=0;combo_action_count=0;cw_count=0;wcw_count=0;cww_count=0
    }
}

$summary=[pscustomobject]@{streams=@((New-SummaryStream))}

# --- 1. the analysis stream view must carry laps + time_semantics ---------------------------------
$meta=@((NF-StreamMeta $summary))
Require ($meta.Count -eq 1) 'NF-StreamMeta must emit one stream view'
$m=$meta[0]
Require ($m.Contains('laps')) 'the analysis stream view MUST carry laps (break #1)'
Require ($null-ne$m.laps -and @($m.laps).Count -eq 2) ('the stream view must carry both laps, got '+[string]@($m.laps).Count)
Require ($m.Contains('time_semantics')) 'the analysis stream view MUST carry time_semantics'
Require ($null-ne$m.time_semantics.movement_window) 'the stream time_semantics must carry the movement window'
Require ($null-eq$m.time_semantics.official_race_duration_s) 'official_race_duration_s must stay null (never invented)'

# --- 2. the estimate must survive into the frontend payload --------------------------------------
$front=Convert-StreamMeta $m
Require ($front.Contains('laps')) 'the frontend payload must carry laps'
Require (@($front.laps).Count -eq 2) ('the frontend payload must carry both laps, got '+[string]@($front.laps).Count)
$l1=@($front.laps)[0]
Require ($null-ne$l1.lap_time_estimate_s) 'lap 1 must expose lap_time_estimate_s to the frontend (the original bug)'
Require ([bool]$l1.lap_time_is_estimate) 'lap 1 must be flagged as an estimate'
Require ([Math]::Abs([double]$l1.lap_time_estimate_s-43.904)-lt0.01) ('lap 1 estimate must be 43.904, got '+[string]$l1.lap_time_estimate_s)
Require ([Math]::Abs([double]$l1.duration_s-52.976)-lt0.001) 'lap 1 raw span must remain 52.976'
Require ([bool]$l1.lap_time_boundary_affected) 'lap 1 must be flagged boundary-affected'
Require ($front.Contains('time_semantics')) 'the frontend payload must carry the stream time_semantics'
Require ([Math]::Abs([double]$front.time_semantics.movement_window_duration_s-89.072)-lt0.001) 'the movement window must reach the frontend'

# --- 3. a lap with NO estimate must stay explicitly unavailable (never a fabricated number) -------
$noEstStream=New-SummaryStream 1 9.072
$noEstLap=@($noEstStream.laps)[0]
$noEstLap.time_semantics.race_time_s_estimate=$null
$noEstLap.time_semantics.race_time_estimated=$false
$noEstLap.time_semantics.race_time_estimated_status='unavailable'
$noEst=[pscustomobject]@{streams=@($noEstStream)}
$fm=Convert-StreamMeta (@((NF-StreamMeta $noEst))[0])
$fn=@($fm.laps)[0]
Require ($null-eq$fn.lap_time_estimate_s) 'a lap without an estimate must expose null, not a number'
Require (-not[bool]$fn.lap_time_is_estimate) 'a lap without an estimate must not be flagged as one'
Require ([Math]::Abs([double]$fn.duration_s-52.976)-lt0.001) 'the raw span must still be present when no estimate exists'

# --- 4. the JS must understand BOTH shapes --------------------------------------------------------
$js=[IO.File]::ReadAllText((Join-Path $AppDir 'Modules\Frontend\js\app.js'))
Require ($js -match 'function lapEstimateOf') 'app.js must define lapEstimateOf'
Require ($js -match 'time_semantics') 'app.js must read the nested telemetry shape (break #2)'
Require ($js -match 'race_time_s_estimate') 'app.js must read time_semantics.race_time_s_estimate'
Require ($js -match 'lap_time_estimate_s') 'app.js must still read the flat training shape'
Require ($js -match 'function lapSpanOf') 'app.js must resolve the raw span in either shape'
Require ($js -match '涨幅|跨度') 'app.js must label a raw span as a span, never as a lap time'
Require ($js -match 'EST_MARK') 'app.js must have an estimate marker'

Write-Host ''
Write-Host ('[OK] Time-semantics data chain contract passed. checks='+$checks+' chain=telemetry->NF-StreamMeta->Convert-StreamMeta->payload laps+time_semantics carried estimate=43.904 available unavailable-stays-null js=both-shapes')
exit 0
