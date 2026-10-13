# Step 0 of the J.U.R.A. install on a clean Windows PC: finds Python 3.11+ (or installs it via winget), then runs install.py.
# install.py installs everything else (Git, Ollama, libraries). ASCII only: Windows PowerShell 5.1 misreads UTF-8 without BOM.
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $InstallArgs)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$m;$u"
}

function Find-Python {
    # returns the path to python.exe 3.11+ or $null; the Microsoft Store stub (WindowsApps) is skipped
    $candidates = @()
    foreach ($name in 'py', 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') { $candidates += $cmd.Source }
    }
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($c in $candidates) {
        try {
            $exe = if ((Split-Path $c -Leaf) -eq 'py.exe') { & $c -3 -c "import sys;print(sys.executable)" 2>$null } else { $c }
            $ok = & $exe -c "import sys;print(int(sys.version_info>=(3,11)))" 2>$null
            if ($ok -eq '1') { return $exe }
        } catch { }
    }
    return $null
}

Write-Host 'J.U.R.A. - preparing: looking for Python 3.11+'
$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host 'Python 3.11+ not found and winget (App Installer) is unavailable.'
        Write-Host 'Download Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run install.cmd again.'
        exit 2
    }
    Write-Host 'Python not found. Installing Python 3.12 via winget (Windows may ask for permission)...'
    winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $python = Find-Python
    if (-not $python) {
        Write-Host 'Python was installed but is not visible in this window. Close it and run install.cmd again.'
        exit 3
    }
}
Write-Host "Python: $python"
Set-Location $root
& $python (Join-Path $root 'scripts\install.py') @InstallArgs
exit $LASTEXITCODE
