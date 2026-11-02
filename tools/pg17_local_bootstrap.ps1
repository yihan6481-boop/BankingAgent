# One-time bootstrap of the local PostgreSQL 17 service for the banking demo.
#
# Creates the restricted application roles, the development/test databases and
# the external passfile used by the project. It asks for the local PostgreSQL 17
# superuser (postgres) password in this window; the password is never printed,
# never written into the project and never passed on a command line.
#
# Run it once in a normal PowerShell window:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\pg17_local_bootstrap.ps1
#
# Existing databases are never dropped or overwritten.
$ErrorActionPreference = 'Stop'
$psql = 'C:\Program Files\PostgreSQL\17\bin\psql.exe'
if (-not (Test-Path -LiteralPath $psql)) { throw 'PostgreSQL 17 psql.exe not found at C:\Program Files\PostgreSQL\17\bin' }
$setup = Join-Path $PSScriptRoot 'pg17_local_setup.sql'
if (-not (Test-Path -LiteralPath $setup)) { throw 'tools/pg17_local_setup.sql is missing' }
$homeRoot = Join-Path $HOME '.banking-agent'
$source = Join-Path $homeRoot 'pg01'
$state = Join-Path $homeRoot 'pg17'
$migPwFile = Join-Path $source 'migration.password'
$runPwFile = Join-Path $source 'runtime.password'
foreach ($file in @($migPwFile, $runPwFile)) {
    if (-not (Test-Path -LiteralPath $file)) { throw "missing external password file: $file" }
}
$migPw = (Get-Content -LiteralPath $migPwFile -Raw).Trim()
$runPw = (Get-Content -LiteralPath $runPwFile -Raw).Trim()
if ($migPw -notmatch '^[A-Za-z0-9_\-]{16,}$' -or $runPw -notmatch '^[A-Za-z0-9_\-]{16,}$') {
    throw 'external role passwords have unexpected characters; refusing to continue'
}
if (-not (Test-Path -LiteralPath $state)) { New-Item -ItemType Directory -Path $state | Out-Null }

Write-Host 'Local PostgreSQL 17 bootstrap for the five-person banking demo'
Write-Host 'Target : 127.0.0.1:5432 (this machine; no Docker)'
Write-Host 'Creates: roles banking_migrator/banking_runtime; databases banking_demo/banking_test/banking_foundation'
$secure = Read-Host -AsSecureString 'Enter the local PostgreSQL 17 superuser (postgres) password'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $adminPw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
$secure = $null
if ([string]::IsNullOrWhiteSpace($adminPw)) { throw 'empty password' }

$env:PGPASSWORD = $adminPw
$env:PGAPPNAME = 'banking_agent_pg17_bootstrap'
$tempScript = Join-Path $env:TEMP ('banking_pg17_bootstrap_' + [guid]::NewGuid().ToString('N') + '.sql')
try {
    $version = & $psql -w -h 127.0.0.1 -p 5432 -U postgres -d postgres -At -c "SELECT current_setting('server_version')"
    if ($LASTEXITCODE -ne 0) { throw 'cannot connect to local PostgreSQL 17 (127.0.0.1:5432) as postgres' }
    $version = $version.Trim()
    if ($version -notmatch '^17\.') { throw "unexpected server version: $version" }
    Write-Host "Connected: PostgreSQL $version"

    $existing = & $psql -w -h 127.0.0.1 -p 5432 -U postgres -d postgres -At -c "SELECT string_agg(datname,',') FROM pg_database WHERE datname IN ('banking_demo','banking_test','banking_foundation')"
    if ($existing -and $existing.Trim()) {
        $tables = & $psql -w -h 127.0.0.1 -p 5432 -U postgres -d banking_demo -At -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')"
        if ($LASTEXITCODE -ne 0 -or [int]$tables -gt 0) {
            throw "target database already contains data ($existing / tables=$tables). Nothing was dropped or overwritten; investigate before restoring."
        }
        Write-Warning "Databases already present and empty: $($existing.Trim()); roles and privileges will be reconciled."
    }

    $body = Get-Content -LiteralPath $setup -Raw -Encoding UTF8
    $header = "\set migration_password '$migPw'`n\set runtime_password '$runPw'`n"
    [IO.File]::WriteAllText($tempScript, $header + $body, (New-Object System.Text.UTF8Encoding($false)))
    & $psql -w -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1 -q -f $tempScript
    if ($LASTEXITCODE -ne 0) { throw 'role/database bootstrap failed' }

    foreach ($db in @('banking_demo', 'banking_test')) {
        & $psql -w -h 127.0.0.1 -p 5432 -U postgres -d $db -v ON_ERROR_STOP=1 -q -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC'
        if ($LASTEXITCODE -ne 0) { throw "schema hardening failed for $db" }
    }
    & $psql -w -h 127.0.0.1 -p 5432 -U postgres -d banking_foundation -v ON_ERROR_STOP=1 -q -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC; ALTER DEFAULT PRIVILEGES FOR ROLE banking_migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC'
    if ($LASTEXITCODE -ne 0) { throw 'schema hardening failed for banking_foundation' }
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:PGAPPNAME -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $tempScript) { Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue }
}

# External passfile (outside the project, excluded from every ZIP).
$passfile = Join-Path $state 'pgpass.conf'
function Escaped([string]$value) { return $value.Replace('\', '\\').Replace(':', '\:') }
$entries = @(
    '127.0.0.1:5432:*:postgres:' + (Escaped $adminPw)
    '127.0.0.1:5432:*:banking_migrator:' + (Escaped $migPw)
    '127.0.0.1:5432:*:banking_runtime:' + (Escaped $runPw)
)
$text = [string]::Join([string][char]10, $entries)
[IO.File]::WriteAllText($passfile, $text + [string][char]10, (New-Object System.Text.UTF8Encoding($false)))
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $passfile /inheritance:r /grant:r ("{0}:(F)" -f $identity) /grant:r 'SYSTEM:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to restrict passfile permissions' }
$adminPw = $null

$env:PGPASSFILE = $passfile
try {
    foreach ($check in @(@('banking_migrator', 'banking_demo'), @('banking_runtime', 'banking_demo'), @('postgres', 'banking_foundation'))) {
        $who = & $psql -w -h 127.0.0.1 -p 5432 -U $check[0] -d $check[1] -At -c 'SELECT current_user'
        if ($LASTEXITCODE -ne 0 -or $who.Trim() -ne $check[0]) { throw "login check failed for $($check[0])" }
    }
}
finally { Remove-Item Env:PGPASSFILE -ErrorAction SilentlyContinue }

$record = [ordered]@{
    host = '127.0.0.1'
    port = 5432
    server_version = $version
    databases = @('banking_demo', 'banking_test', 'banking_foundation')
    roles = @('postgres', 'banking_migrator', 'banking_runtime')
    passfile = '~/.banking-agent/pg17/pgpass.conf'
    bootstrapped_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}
[IO.File]::WriteAllText((Join-Path $state 'setup.json'), ($record | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
Write-Host 'OK: local PostgreSQL 17 roles, databases and external passfile are ready.'
Write-Host ('State record: ' + (Join-Path $state 'setup.json'))
Write-Host 'Next step is the verified restore of the Docker backup into banking_demo (run by the agent).'
