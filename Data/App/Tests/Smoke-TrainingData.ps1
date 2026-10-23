param([string]$AppDir=(Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$ErrorActionPreference='Stop'
$python=Get-Command python -ErrorAction SilentlyContinue
if($null-eq$python){Write-Host '[SKIP] Training dataset tests require optional Python + NumPy.';exit 0}
& $python.Source -c 'import numpy' 2>$null
if($LASTEXITCODE-ne0){Write-Host '[SKIP] Training dataset tests require optional NumPy.';exit 0}
& $python.Source (Join-Path $AppDir 'Tests/training_data/test_replay_dataset.py')
if($LASTEXITCODE-ne0){throw 'Training dataset learning-contract tests failed.'}
Write-Host '[OK] Training dataset learning contracts passed.'
