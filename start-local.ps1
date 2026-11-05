$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'deploy/local/setup.ps1')
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$runPath = Join-Path $PSScriptRoot 'deploy/local/run.py'
$pidPath = Join-Path $PSScriptRoot 'runtime/local/server.pid'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create the local .venv and install deploy/server/requirements.txt first; see deploy/local/README.md.'
}
if (Test-Path -LiteralPath $pidPath) {
    $serverPid = [int](Get-Content -LiteralPath $pidPath)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.ExecutablePath -eq $pythonPath -and $existing.CommandLine.Contains($runPath)) {
        Write-Output 'Already running: https://localhost:18443'
        return
    }
}
$server = Start-Process -FilePath $pythonPath -ArgumentList @('-X', 'utf8', ('"' + $runPath + '"')) `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $PSScriptRoot 'runtime/local/logs/server.out.log') `
    -RedirectStandardError (Join-Path $PSScriptRoot 'runtime/local/logs/server.err.log')
[System.IO.File]::WriteAllText($pidPath, [string]$server.Id)
$readyCode = 'import httpx,sys; response=httpx.get("https://127.0.0.1:18443/login", verify=False, trust_env=False, timeout=2); sys.exit(0 if response.status_code == 200 else 1)'
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    & $pythonPath -X utf8 -c $readyCode 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "Started local server (PID $($server.Id)): https://localhost:18443"
        Write-Output 'Login details: runtime/local/ACCESS.txt'
        return
    }
    Start-Sleep -Milliseconds 250
}
throw 'The local server did not become ready; inspect runtime/local/logs/server.err.log.'
