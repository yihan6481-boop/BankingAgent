$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
# Relocated complete ZIP may reuse this machine's existing configuration without
# copying secrets into the archive or replacing the original .env.
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    $configHint = Join-Path $PSScriptRoot 'tools\local_config_path.txt'
    if (Test-Path -LiteralPath $configHint) {
        $existingConfig = (Get-Content -LiteralPath $configHint -Encoding UTF8 -Raw).Trim()
        if ($existingConfig -and (Test-Path -LiteralPath $existingConfig -PathType Leaf)) {
            foreach ($configLine in (Get-Content -LiteralPath $existingConfig -Encoding UTF8)) {
                $configPair = $configLine.Trim() -split '=', 2
                if ($configPair.Count -eq 2 -and $configPair[0].Trim() -match '^[A-Za-z_][A-Za-z0-9_]*$') {
                    $configName = $configPair[0].Trim()
                    if (-not (Test-Path -LiteralPath ('Env:' + $configName))) {
                        Set-Item -LiteralPath ('Env:' + $configName) -Value $configPair[1].Trim().Trim('"', "'")
                    }
                }
            }
            Write-Host 'Config: reusing original local configuration (not copied into package).'
        }
    }
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $hint = Join-Path $PSScriptRoot 'tools\local_python_path.txt'
    if (Test-Path -LiteralPath $hint) {
        $existingPython = (Get-Content -LiteralPath $hint -Encoding UTF8 -Raw).Trim()
        if ($existingPython -and (Test-Path -LiteralPath $existingPython)) { $python = $existingPython }
    }
}
if (-not (Test-Path $python)) { $python = 'python' }
& $python -X utf8 (Join-Path $PSScriptRoot 'tools/run_integrated.py') @args
if ($LASTEXITCODE -ne 0) { throw 'Startup failed. See output above.' }
