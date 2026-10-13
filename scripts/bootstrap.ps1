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

function Install-PythonDirect {
    # clean Windows 10 often has no winget: take the official installer from python.org, per-user, no admin needed
    $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    $exe = Join-Path $env:TEMP 'jura-python-setup.exe'
    Write-Host "Downloading Python 3.12 from $url ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'   # PowerShell 5.1 downloads very slowly with the progress bar
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    Write-Host 'Installing Python 3.12 (quiet, for the current user; usually a few minutes, up to 30 on a slow PC)...'
    $log = Join-Path $env:TEMP 'jura-python-install.log'
    # WaitForExit, not -Wait: in PowerShell 5.1 -Wait also waits for every child the installer leaves running and can hang forever
    $p = Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0', 'Include_launcher=0', 'Include_doc=0', 'Include_tcltk=0', 'Include_dev=0', '/log', "`"$log`"" -PassThru
    if (-not $p.WaitForExit(1800000)) { Write-Host "Python installer did not finish in 30 minutes, see $log"; return -2 }
    Remove-Item $exe -ErrorAction SilentlyContinue
    return $p.ExitCode
}

Write-Host 'J.U.R.A. - preparing: looking for Python 3.11+'
$python = Find-Python
if (-not $python) {
    $viaWinget = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host 'Python not found. Installing Python 3.12 via winget (Windows may ask for permission)...'
        winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $python = Find-Python
        $viaWinget = [bool]$python
    }
    if (-not $viaWinget) {
        try { $code = Install-PythonDirect } catch { $code = -1; Write-Host "Download failed: $($_.Exception.Message)" }
        Refresh-Path
        $python = Find-Python
        if (-not $python) {
            Write-Host "Python could not be installed automatically (code $code)."
            Write-Host 'Install it from https://www.python.org/downloads/ (tick Add python.exe to PATH) and run install.cmd again.'
            exit 2
        }
    }
}
Write-Host "Python: $python"
Set-Location $root
& $python (Join-Path $root 'scripts\install.py') @InstallArgs
exit $LASTEXITCODE
