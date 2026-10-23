<#
.SYNOPSIS
Export native SAV playback input snapshots without starting the game.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string[]]$ReplayPath,
    [string]$OutDir='',
    [string]$PythonCommand='python'
)
$ErrorActionPreference='Stop'
function Get-InputReplaySha256 {
    param([string]$Path)
    $algorithm=[Security.Cryptography.SHA256]::Create()
    $stream=[IO.File]::OpenRead($Path)
    try{return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}
    finally{$stream.Dispose();$algorithm.Dispose()}
}
$appDir=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$projectRoot=(Resolve-Path -LiteralPath (Join-Path $appDir '../..')).Path
$scratchRoot=[IO.Path]::GetFullPath((Join-Path $projectRoot '.temp/replay-input-cache'))
$outputRoot=[IO.Path]::GetFullPath((Join-Path $projectRoot '.temp'))
if([string]::IsNullOrWhiteSpace($OutDir)){$OutDir=Join-Path $outputRoot 'replay-inputs'}
$OutDir=[IO.Path]::GetFullPath($OutDir)
if(-not $OutDir.StartsWith($outputRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
    throw 'Input exports must stay inside the project .temp directory.'
}
if($OutDir.Equals($scratchRoot,[StringComparison]::OrdinalIgnoreCase) -or $OutDir.StartsWith($scratchRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
    throw 'The output cannot overwrite extraction caches.'
}
if((Test-Path -LiteralPath $OutDir) -and @(Get-ChildItem -LiteralPath $OutDir -Force).Count -gt 0){
    throw 'Output directory is not empty. Choose another -OutDir.'
}
$inputs=New-Object System.Collections.Generic.List[object]
foreach($path in $ReplayPath){
    $item=Get-Item -LiteralPath $path
    if($item.PSIsContainer){
        foreach($file in @(Get-ChildItem -LiteralPath $item.FullName -File -Filter '*.sav' | Sort-Object Name)){$inputs.Add($file)}
    }elseif($item.Extension -ieq '.sav'){$inputs.Add($item)}else{throw 'Inputs must be SAV files or directories containing SAV files.'}
}
if($inputs.Count-eq0){throw 'No SAV recordings found.'}
foreach($file in $inputs){
    if($file.FullName.StartsWith($OutDir+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
        throw 'An output directory cannot contain source recordings.'
    }
}
& $PythonCommand -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ is required"'
if($LASTEXITCODE-ne0){throw 'Python 3.10+ is required.'}
$runtime=Join-Path $scratchRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$originalTemp=$env:TEMP;$originalTmp=$env:TMP
$env:TEMP=$runtime;$env:TMP=$runtime
try{
    . (Join-Path $appDir 'Modules/Telemetry/Telemetry.CSharpCore.ps1')
    Initialize-TelemetryCSharpCore -AppDir $appDir
    $sources=New-Object System.Collections.Generic.List[object]
    $seen=New-Object 'System.Collections.Generic.HashSet[string]'
    foreach($file in $inputs){
        $sha=Get-InputReplaySha256 -Path $file.FullName
        if(-not $seen.Add($sha)){continue}
        # Always detect from the current source; no stale manifest is trusted.
        $cache=Join-Path $scratchRoot ($sha+'-'+[Guid]::NewGuid().ToString('N'))
        [QQReplayPortable]::ExtractFast([string[]]@($file.FullName),$cache)
        $sources.Add([ordered]@{replay_path=$file.FullName;sha256=$sha;physical_manifest=(Join-Path $cache 'manifest.json')})
    }
    $indexPath=Join-Path $scratchRoot ('sources-'+[Guid]::NewGuid().ToString('N')+'.json')
    [IO.File]::WriteAllText($indexPath,(ConvertTo-Json -InputObject @($sources.ToArray()) -Depth 5),(New-Object Text.UTF8Encoding($false)))
    & $PythonCommand (Join-Path $PSScriptRoot 'export_inputs.py') --sources $indexPath --out $OutDir
    if($LASTEXITCODE-ne0){throw 'Input extraction failed. See the source index under .temp/replay-input-cache.'}
    Write-Host ('[OK] Native replay inputs: '+$OutDir)
}finally{
    $env:TEMP=$originalTemp;$env:TMP=$originalTmp
}
