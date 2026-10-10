# Atomic write: a unique temp file in the SAME directory, then a true atomic replace
# (ReplaceFile). A crash can never leave a half-written cache/summary, and - unlike
# "delete then move" - a concurrent reader can never observe a moment where the
# destination does not exist. The catalog writer (Modules/Replay/Replay.Catalog.ps1,
# Write-JsonFileAtomic) carries the identical contract.
function Write-JsonUtf8([string]$Path,[object]$Value,[int]$Depth=12) {
    $dir=Split-Path -Parent $Path
    if($dir){New-Item -ItemType Directory -Force -Path $dir|Out-Null}
    $tmp=$Path+'.'+[string]$PID+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    $enc=New-Object System.Text.UTF8Encoding -ArgumentList $true
    try {
        [IO.File]::WriteAllText($tmp,($Value|ConvertTo-Json -Depth $Depth),$enc)
        if(-not[IO.File]::Exists($Path)){
            [IO.File]::Move($tmp,$Path)
            return
        }
        # [NullString]::Value: passing $null marshals to an empty string, which ReplaceFile rejects.
        $lastError=$null
        for($attempt=1;$attempt -le 6;$attempt++){
            try { [IO.File]::Replace($tmp,$Path,[NullString]::Value,$true); return }
            catch { $lastError=$_; Start-Sleep -Milliseconds 25 }
        }
        throw ('Atomic replace of '+$Path+' failed: '+$lastError.Exception.Message)
    } catch {
        if([IO.File]::Exists($tmp)){Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
        throw
    }
}
function Safe-Number([double]$v,[double]$fallback=0.0) {
    if([double]::IsNaN($v) -or [double]::IsInfinity($v)){return $fallback}
    return $v
}
function Normalize-Angle([double]$a) {
    $pi=[Math]::PI
    while($a -gt $pi){$a-=2.0*$pi}
    while($a -lt -$pi){$a+=2.0*$pi}
    return $a
}
function Median-Value([double[]]$Values) {
    if($null -eq $Values -or $Values.Count -eq 0){ return 0.0 }
    $a=@($Values|Sort-Object);$n=$a.Count
    if(($n % 2)-eq 1){ return [double]$a[[int]($n/2)] }
    return ([double]$a[$n/2-1]+[double]$a[$n/2])/2.0
}

# ---------------------------------------------------------------------------------------------
# Replay time semantics (see docs: Output/Handoff/REPLAY_TIME_AUDIT.md).
#
# A replay's frame span is NOT race time. A recording normally opens with the countdown / pre-race
# idle (the car parked) and closes with post-finish footage (the car parked again). The frame span
# therefore overstates race time, and charging that span to the opening lap makes the opening lap
# look slower than it was - which also corrupts A/B lap deltas between two replays whose parked
# heads differ.
#
# THE CONTRACT:
#   recording_duration_s  the full frame span            - a measurement, never called race time
#   movement_window_*     first..last frame with movement - an ESTIMATE bounded by motion evidence
#   official_race_duration_s  only when a trustworthy official timing source exists, else $null
#
# A parked car is only treated as non-racing at the RECORDING EDGES. Stopping anywhere inside the
# movement window (a crash, a spin, waiting) is racing time and is never removed.
#
# Nothing here rescales, retimes or reinterprets native time_ms or lap_index. The raw timeline is
# untouched; only the derived duration/estimate fields are added.
# ---------------------------------------------------------------------------------------------
$script:QQReplayMovementSpeedMps=0.5

function Get-QQReplayMovementWindow {
    param([object[]]$Rows,[string]$Source='production_telemetry')
    $out=[ordered]@{
        available=$false
        reason='no_movement_evidence'
        speed_threshold_mps=$script:QQReplayMovementSpeedMps
        first_moving_index=$null
        last_moving_index=$null
        start_t=$null
        end_t=$null
        duration_s=$null
        head_idle_s=$null
        tail_idle_s=$null
        frames=$null
        speed_source='replay_linear_velocity'
        source=$Source
    }
    if($null-eq$Rows -or @($Rows).Count-eq0){ $out.reason='no_rows'; return [pscustomobject]$out }
    $n=@($Rows).Count
    $first=-1;$last=-1
    for($i=0;$i-lt$n;$i++){
        $r=$Rows[$i]
        if(-not[bool]$r.pose_valid){continue}
        $v=[double]$r.speed
        if([double]::IsNaN($v)-or[double]::IsInfinity($v)){continue}
        if($v-ge$script:QQReplayMovementSpeedMps){
            if($first-lt0){$first=$i}
            $last=$i
        }
    }
    if($first-lt0 -or $last-lt$first){ return [pscustomobject]$out }
    $t0=[double]$Rows[0].time_s; $t1=[double]$Rows[$n-1].time_s
    $ms=[double]$Rows[$first].time_s; $me=[double]$Rows[$last].time_s
    $out.available=$true
    $out.reason='movement_window_from_speed_threshold'
    $out.first_moving_index=$first
    $out.last_moving_index=$last
    $out.start_t=[Math]::Round($ms,4)
    $out.end_t=[Math]::Round($me,4)
    $out.duration_s=[Math]::Round($me-$ms,4)
    $out.head_idle_s=[Math]::Round($ms-$t0,4)
    $out.tail_idle_s=[Math]::Round($t1-$me,4)
    $out.frames=$n
    return [pscustomobject]$out
}

function Get-QQReplayLapTimeSemantics {
    # $SpanStartT/$SpanEndT = the lap's raw frame span; $Window = recording movement window.
    # race_time_s_estimate is the part of the lap span that lies inside the movement window, so
    # recording-edge idle is excluded while any in-lap stop is preserved.
    param([double]$SpanStartT,[double]$SpanEndT,[object]$Window,[bool]$BoundaryAffected=$false)
    $span=[Math]::Round($SpanEndT-$SpanStartT,4)
    $out=[ordered]@{
        contract='replay_time_semantics_v1'
        lap_span_s=$span
        boundary_affects_time=$BoundaryAffected
        race_time_s_estimate=$null
        race_time_estimated=$false
        race_time_estimated_status='unavailable'
        excludes_edge_idle_s=$null
        reason='movement_window_unavailable'
    }
    if($null-eq$Window -or -not[bool]$Window.available){ return $out }
    $ws=[double]$Window.start_t; $we=[double]$Window.end_t
    $los=[Math]::Max($SpanStartT,$ws); $hie=[Math]::Min($SpanEndT,$we)
    if($hie-lt$los){ $out.reason='lap_outside_movement_window'; return $out }
    $est=[Math]::Round($hie-$los,4)
    $out.race_time_s_estimate=$est
    $out.race_time_estimated=$true
    # An estimate is only usable when the lap actually moved inside the window.
    if($est-le0){ $out.race_time_estimated=$false; $out.race_time_estimated_status='unavailable'; $out.reason='no_movement_inside_lap'; return $out }
    # Fail closed if motion evidence covers an implausibly small part of the recorded span.
    $spanDuration=[double]$Window.head_idle_s+[double]$Window.duration_s+[double]$Window.tail_idle_s
    if($spanDuration-gt0 -and ([double]$Window.duration_s/$spanDuration)-lt0.25){
        $out.race_time_estimated=$false; $out.race_time_estimated_status='unavailable'; $out.reason='movement_window_implausibly_short'
        return $out
    }
    $out.race_time_estimated_status='estimated_from_movement'
    $out.excludes_edge_idle_s=[Math]::Round($span-$est,4)
    $out.reason=''
    return $out
}

function Build-NativeLapSegments($Clean) {
    # v3: Lap segmentation is a replay-native fact (record +76), never inferred
    # from returning near the starting position.
    $rows=@($Clean)
    if($rows.Count-eq0){return @()}
    $valid=@($rows|Where-Object{$null-ne$_.lap_index})
    if($valid.Count-ne$rows.Count){return @()}
    # Motion evidence is computed once over the whole recording, then each lap's overlap with it is
    # reported. The raw span (duration_s) keeps its exact previous meaning.
    $window=Get-QQReplayMovementWindow -Rows $rows -Source 'replay_native_lap_segments'
    $lastLapIndex=[int]$rows[$rows.Count-1].lap_index
    $segments=New-Object System.Collections.Generic.List[object]
    $start=0;$current=[int]$rows[0].lap_index
    for($i=1;$i-lt$rows.Count;$i++){
        $v=[int]$rows[$i].lap_index
        if($v-eq$current){continue}
        $end=$i-1
        if($end-ge$start){
            $sd=[double]$rows[$start].distance;$ed=[double]$rows[$end].distance
            $st=[double]$rows[$start].time_s;$et=[double]$rows[$end].time_s
            $boundary=($current-eq[int]$rows[0].lap_index)-or($current-eq$lastLapIndex)
            $segments.Add([ordered]@{lap=$current;start_i=$start;end_i=$end;start_t=[Math]::Round($st,4);end_t=[Math]::Round($et,4);duration_s=[Math]::Round(($et-$st),4);start_distance=[Math]::Round($sd,3);end_distance=[Math]::Round($ed,3);distance=[Math]::Round(($ed-$sd),3);source='replay_native_lap_index';recording_duration_s=($null);movement_window=$window;time_semantics=(Get-QQReplayLapTimeSemantics -SpanStartT $st -SpanEndT $et -Window $window -BoundaryAffected $boundary)})
        }
        $start=$i;$current=$v
    }
    $end=$rows.Count-1
    if($end-ge$start){
        $sd=[double]$rows[$start].distance;$ed=[double]$rows[$end].distance
        $st=[double]$rows[$start].time_s;$et=[double]$rows[$end].time_s
        $boundary=($current-eq[int]$rows[0].lap_index)-or($current-eq$lastLapIndex)
        $segments.Add([ordered]@{lap=$current;start_i=$start;end_i=$end;start_t=[Math]::Round($st,4);end_t=[Math]::Round($et,4);duration_s=[Math]::Round(($et-$st),4);start_distance=[Math]::Round($sd,3);end_distance=[Math]::Round($ed,3);distance=[Math]::Round(($ed-$sd),3);source='replay_native_lap_index';recording_duration_s=($null);movement_window=$window;time_semantics=(Get-QQReplayLapTimeSemantics -SpanStartT $st -SpanEndT $et -Window $window -BoundaryAffected $boundary)})
    }
    return $segments.ToArray()
}
