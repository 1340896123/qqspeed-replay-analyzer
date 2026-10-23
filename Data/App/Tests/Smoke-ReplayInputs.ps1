param([string]$AppDir=(Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$ErrorActionPreference='Stop'
$python=Get-Command python -ErrorAction SilentlyContinue
if($null-eq$python){Write-Host '[SKIP] Replay input export tests require optional Python 3.10+.';exit 0}
& $python.Source (Join-Path $AppDir 'Tests/replay_inputs/test_replay_inputs.py')
if($LASTEXITCODE-ne0){throw 'Native playback input export contracts failed.'}
Write-Host '[OK] Native playback input export contracts passed.'
