<#
.SYNOPSIS
Extract reliable SAV facts and build masked trajectory training sequences.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File Data/App/Tools/TrainingData/Export-AutodriveDataset.ps1 -ReplayPath <replay-directory>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string[]]$ReplayPath,
    [string]$OutDir='',
    [string]$PythonCommand='python',
    [ValidateRange(1,240)][double]$Hz=20,
    [ValidateRange(2,1000)][int]$History=10,
    [ValidateRange(1,1000)][int]$Future=20,
    [ValidateRange(1,10000)][int]$Stride=5,
    [ValidateRange(1,1000)][double]$MaxGapMs=150,
    [int]$Seed=42,
    [switch]$IncludeNetwork
)
$ErrorActionPreference='Stop'
$appDir=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$projectRoot=(Resolve-Path -LiteralPath (Join-Path $appDir '../..')).Path
$scratchRoot=[IO.Path]::GetFullPath((Join-Path $projectRoot '.temp/training-data-cache'))
if([string]::IsNullOrWhiteSpace($OutDir)){$OutDir=Join-Path $projectRoot '.temp/training-data'}
$OutDir=[IO.Path]::GetFullPath($OutDir)
if(-not $OutDir.StartsWith($projectRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
    throw 'Training outputs must stay inside the project directory.'
}
foreach($protected in @($appDir,(Join-Path $projectRoot '.git'),$scratchRoot)){
    if($OutDir.Equals($protected,[StringComparison]::OrdinalIgnoreCase) -or $OutDir.StartsWith($protected+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
        throw 'The output cannot overwrite source, Git metadata or extraction caches.'
    }
}
if((Test-Path -LiteralPath $OutDir) -and @(Get-ChildItem -LiteralPath $OutDir -Force).Count -gt 0){
    throw 'Output directory is not empty. Choose a new -OutDir; existing datasets are preserved.'
}
& $PythonCommand -c "import numpy; print('NumPy ' + numpy.__version__)"
if($LASTEXITCODE-ne0){throw 'Python with NumPy is required. See Tools/TrainingData/requirements.txt.'}
$inputs=New-Object System.Collections.Generic.List[object]
foreach($path in $ReplayPath){
    $item=Get-Item -LiteralPath $path
    if($item.PSIsContainer){
        foreach($file in @(Get-ChildItem -LiteralPath $item.FullName -File -Filter '*.sav' | Sort-Object Name)){$inputs.Add($file)}
    }elseif($item.Extension -ieq '.sav'){$inputs.Add($item)}else{throw 'Inputs must be SAV files or directories containing SAV files.'}
}
if($inputs.Count-eq0){throw 'No SAV recordings found.'}
foreach($inputFile in $inputs){
    if($inputFile.FullName.StartsWith($OutDir+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
        throw 'An output directory cannot contain source recordings.'
    }
}
New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null
$runtime=Join-Path $scratchRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
# Compilers and child smoke tools keep intermediate files under the project.
$originalTemp=$env:TEMP;$originalTmp=$env:TMP
$env:TEMP=$runtime;$env:TMP=$runtime
try{
    . (Join-Path $appDir 'Modules/Telemetry/Telemetry.CSharpCore.ps1')
    Initialize-TelemetryCSharpCore -AppDir $appDir
    $corePath=Join-Path $appDir 'Modules/Telemetry/QQReplayTelemetry.Core.cs'
    $contractPath=Join-Path $appDir 'Modules/Telemetry/Core/ReplayCore.Contracts.cs'
    $coreHash=(Get-FileHash -LiteralPath $corePath -Algorithm SHA256).Hash.Substring(0,8)+(Get-FileHash -LiteralPath $contractPath -Algorithm SHA256).Hash.Substring(0,8)
    $sources=New-Object System.Collections.Generic.List[object]
    $seen=New-Object 'System.Collections.Generic.HashSet[string]'
    foreach($file in $inputs){
        $sha=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if(-not $seen.Add($sha)){continue}
        $cache=[IO.Path]::GetFullPath((Join-Path $scratchRoot $sha))
        if(-not $cache.StartsWith($scratchRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Invalid cache target.'}
        $telemetry=Join-Path $cache 'telemetry'
        $physical=Join-Path $telemetry 'physical'
        $actions=Join-Path $telemetry 'native_actions'
        $raw=Join-Path $cache ('physical_csv_'+$coreHash)
        New-Item -ItemType Directory -Force -Path $cache | Out-Null
        $log=Join-Path $cache 'telemetry.log'
        Write-Host ('[Extract] '+$sha.Substring(0,12))
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $appDir 'QQReplayTelemetry.ps1') -ReplayPath $file.FullName -OutDir $telemetry -PhysicalCacheDir $physical -NativeActionCacheRoot $actions *> $log
        if($LASTEXITCODE-ne0){throw ('Telemetry extraction failed; see '+$log)}
        # This existing compatibility export retains quaternion, native velocity and raw
        # candidate bytes omitted by the compact logical CSV. No new native parser is used.
        $rawManifest=Join-Path $raw 'manifest.json'
        if(-not (Test-Path -LiteralPath $rawManifest)){
            [QQReplayPortable]::Extract([string[]]@($file.FullName),$raw)
        }
        $after=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if($after-ne$sha){throw 'Source changed during extraction; no dataset was published.'}
        $sources.Add([ordered]@{sha256=$sha;telemetry_summary=(Join-Path $telemetry 'telemetry_summary.json');physical_manifest=$rawManifest})
    }
    $indexPath=Join-Path $scratchRoot ('sources_'+[Guid]::NewGuid().ToString('N')+'.json')
    $json=ConvertTo-Json -InputObject @($sources.ToArray()) -Depth 5
    [IO.File]::WriteAllText($indexPath,$json,(New-Object Text.UTF8Encoding($false)))
    $inv=[Globalization.CultureInfo]::InvariantCulture
    $buildArgs=@((Join-Path $PSScriptRoot 'build_dataset.py'),'--sources',$indexPath,'--out',$OutDir,
                 '--hz',$Hz.ToString($inv),'--history',[string]$History,'--future',[string]$Future,
                 '--stride',[string]$Stride,'--max-gap-ms',$MaxGapMs.ToString($inv),'--seed',[string]$Seed)
    if($IncludeNetwork){$buildArgs+='--include-network'}
    & $PythonCommand @buildArgs
    if($LASTEXITCODE-ne0){throw 'Dataset conversion failed. See the extraction index and logs under .temp/training-data-cache.'}
    Write-Host ('[OK] Training dataset: '+$OutDir)
}finally{
    $env:TEMP=$originalTemp;$env:TMP=$originalTmp
}
