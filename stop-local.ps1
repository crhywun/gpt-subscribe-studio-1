$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $PSScriptRoot 'runtime/local/server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Output 'No local server PID file exists.'
    return
}
$serverPid = [int](Get-Content -LiteralPath $pidPath)
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$runPath = Join-Path $PSScriptRoot 'deploy/local/run.py'
$server = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
if ($server) {
    if ($server.ExecutablePath -ne $pythonPath -or -not $server.CommandLine.Contains($runPath)) {
        throw 'PID belongs to another process; no process was stopped.'
    }
    # Windows venv python.exe launches the base interpreter as a child.
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $serverPid"
    foreach ($child in $children) {
        if ($child.Name -in @('python.exe', 'pythonw.exe') -and $child.CommandLine -and $child.CommandLine.Contains($runPath)) {
            Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Stop-Process -Id $serverPid -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $pidPath
Write-Output 'Local server stopped. Data and proxy settings are preserved.'
