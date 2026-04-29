# install_python.ps1 - Detect or auto-install Python 3.10+ (robust v3, AV-aware)
# Outputs: Python command line to .python_cmd.txt
# Exit codes: 0 = success, 1 = failure

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutFile = Join-Path $ScriptDir '.python_cmd.txt'

$PythonVersion = '3.12.7'

$PythonMirrors = @(
    "https://mirrors.huaweicloud.com/python/$PythonVersion/python-$PythonVersion-amd64.exe",
    "https://mirrors.aliyun.com/python-release/windows/python-$PythonVersion-amd64.exe",
    "https://registry.npmmirror.com/-/binary/python/$PythonVersion/python-$PythonVersion-amd64.exe",
    "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
)

function Test-PythonExe {
    param([string]$exePath, [switch]$Permissive)
    if (-not $exePath -or -not (Test-Path $exePath)) { return $null }

    # File sanity check first
    $fileInfo = Get-Item $exePath -ErrorAction SilentlyContinue
    if (-not $fileInfo -or $fileInfo.Length -lt 50000) {
        return $null
    }

    # Method 1: PowerShell direct invoke
    $verLine = $null
    try {
        $verLine = & $exePath -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' 2>$null
    } catch {}

    # Method 2: cmd /c invoke (bypasses some PowerShell quirks)
    if (-not $verLine -or $LASTEXITCODE -ne 0) {
        try {
            $verLine = cmd /c "`"$exePath`" -c `"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')`"" 2>$null
        } catch {}
    }

    if ($verLine) {
        $parts = $verLine.Trim().Split('.')
        if ($parts.Count -ge 2) {
            try {
                $major = [int]$parts[0]; $minor = [int]$parts[1]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
                    return $verLine.Trim()
                }
            } catch {}
        }
    }

    # Permissive: file exists + sibling DLL exists -> trust it
    if ($Permissive) {
        $sibling = Join-Path (Split-Path $exePath -Parent) 'python3.dll'
        if (Test-Path $sibling) {
            return 'unknown'
        }
    }

    return $null
}

function Test-PythonCmd {
    param([string[]]$cmd)
    try {
        $verLine = & $cmd[0] $cmd[1..($cmd.Count-1)] -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $verLine) { return $null }
        $parts = $verLine.Trim().Split('.')
        if ($parts.Count -lt 2) { return $null }
        $major = [int]$parts[0]; $minor = [int]$parts[1]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
            return $verLine.Trim()
        }
        return $null
    } catch {
        return $null
    }
}

function Find-PythonViaRegistry {
    $regPaths = @(
        'HKCU:\SOFTWARE\Python\PythonCore',
        'HKLM:\SOFTWARE\Python\PythonCore',
        'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore'
    )
    foreach ($base in $regPaths) {
        if (-not (Test-Path $base)) { continue }
        $versions = Get-ChildItem $base -ErrorAction SilentlyContinue
        foreach ($v in $versions) {
            $installPathKey = Join-Path $v.PSPath 'InstallPath'
            if (Test-Path $installPathKey) {
                try {
                    $installPath = (Get-ItemProperty $installPathKey -ErrorAction SilentlyContinue).'(default)'
                    if ($installPath) {
                        $exe = Join-Path $installPath 'python.exe'
                        $ver = Test-PythonExe $exe
                        if ($ver) { return @{ Cmd = "`"$exe`""; Version = $ver; ExePath = $exe } }
                    }
                } catch {}
            }
        }
    }
    return $null
}

function Find-PythonInCommonPaths {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312_new\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python310\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "${env:ProgramFiles(x86)}\Python312\python.exe",
        "${env:ProgramFiles(x86)}\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe",
        "C:\Python313\python.exe"
    )
    foreach ($p in $candidates) {
        $ver = Test-PythonExe $p
        if ($ver) { return @{ Cmd = "`"$p`""; Version = $ver; ExePath = $p } }
    }
    return $null
}

function Find-PythonByDeepSearch {
    $searchRoots = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles",
        "${env:ProgramFiles(x86)}",
        'C:\'
    )
    foreach ($root in $searchRoots) {
        if (-not (Test-Path $root)) { continue }
        try {
            $hits = Get-ChildItem -Path $root -Filter 'python.exe' -Recurse -Depth 3 -ErrorAction SilentlyContinue -Force |
                    Where-Object { $_.FullName -notlike '*\WindowsApps\*' -and $_.FullName -notlike '*\.venv\*' -and $_.FullName -notlike '*\venv\*' } |
                    Select-Object -First 10
            foreach ($h in $hits) {
                $ver = Test-PythonExe $h.FullName
                if ($ver) { return @{ Cmd = "`"$($h.FullName)`""; Version = $ver; ExePath = $h.FullName } }
            }
        } catch {}
    }
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $ver = Test-PythonCmd @('py', '-3')
        if ($ver) { return @{ Cmd = 'py -3'; Version = $ver } }
    }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notlike '*\WindowsApps\*') {
        $ver = Test-PythonCmd @('python')
        if ($ver) { return @{ Cmd = 'python'; Version = $ver } }
    }
    $found = Find-PythonInCommonPaths
    if ($found) { return $found }
    $found = Find-PythonViaRegistry
    if ($found) { return $found }
    $found = Find-PythonByDeepSearch
    if ($found) { return $found }
    return $null
}

function Clear-IncompleteInstall {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) { return $true }
    $exe = Join-Path $Dir 'python.exe'
    if (Test-Path $exe) {
        return $true
    }
    Write-Host "    Cleaning leftover from previous incomplete install at: $Dir"
    try {
        Remove-Item -Path $Dir -Recurse -Force -ErrorAction Stop
        Start-Sleep -Seconds 1
        Write-Host '    Cleaned.'
        return $true
    } catch {
        Write-Host "    [WARN] Cannot remove (some files locked): $($_.Exception.Message)"
        return $false
    }
}

function Download-WithMirrors {
    param([string]$DestPath)
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    foreach ($url in $PythonMirrors) {
        Write-Host "    Trying: $url"
        $wc = $null
        try {
            $wc = New-Object System.Net.WebClient
            $task = $wc.DownloadFileTaskAsync($url, $DestPath)
            $timeoutMs = 300000
            if ($task.Wait($timeoutMs)) {
                if ((Test-Path $DestPath) -and ((Get-Item $DestPath).Length -gt 1000000)) {
                    Write-Host "    Downloaded $((Get-Item $DestPath).Length) bytes"
                    return $true
                }
            } else {
                Write-Host '    [timeout, trying next]'
                if (Test-Path $DestPath) { Remove-Item $DestPath -Force -ErrorAction SilentlyContinue }
            }
        } catch {
            Write-Host "    [failed: $($_.Exception.Message)]"
            if (Test-Path $DestPath) { Remove-Item $DestPath -Force -ErrorAction SilentlyContinue }
        } finally {
            if ($wc) { $wc.Dispose() }
        }
    }
    return $false
}

function Install-Python {
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    $primaryDir = "$env:LOCALAPPDATA\Programs\Python\Python312"
    $altDir = "$env:LOCALAPPDATA\Programs\Python\Python312_new"

    Write-Host ''
    Write-Host "    No suitable Python found. Will download Python $PythonVersion (about 28MB)."
    $reply = Read-Host '    Continue? [Y/n]'
    if ($reply -and $reply.Trim().ToLower() -notin @('', 'y', 'yes')) {
        Write-Host '    Cancelled.'
        return $null
    }

    # Clean up any previous broken install
    $primaryClean = Clear-IncompleteInstall -Dir $primaryDir
    $targetDir = if ($primaryClean) { $primaryDir } else {
        Write-Host "    Will install to alternate path: $altDir"
        Clear-IncompleteInstall -Dir $altDir | Out-Null
        $altDir
    }

    Write-Host '    Downloading from mirrors...'
    if (Test-Path $installer) { Remove-Item $installer -Force -ErrorAction SilentlyContinue }

    $ok = Download-WithMirrors -DestPath $installer
    if (-not $ok) {
        Write-Host ''
        Write-Host '    [ERROR] All mirrors failed.'
        return $null
    }

    Write-Host "    Installing (target: $targetDir)..."
    Write-Host '    (silent, may take 30-90 seconds)'

    $installArgs = @(
        '/quiet',
        'InstallAllUsers=0',
        'PrependPath=1',
        'Include_test=0',
        'Include_doc=0',
        'Include_launcher=1',
        "TargetDir=$targetDir"
    )
    $proc = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
    Remove-Item $installer -ErrorAction SilentlyContinue
    Write-Host "    Installer exit code: $($proc.ExitCode)"

    Start-Sleep -Seconds 3

    # Try detecting up to 5 times
    Write-Host '    Verifying installation...'
    for ($i = 1; $i -le 5; $i++) {
        $found = Find-Python
        if ($found) {
            Write-Host "    Detected on attempt $i : Python $($found.Version)"
            return $found
        }
        if ($i -lt 5) {
            Write-Host "    Attempt $i : not yet detected, waiting 3s..."
            Start-Sleep -Seconds 3
        }
    }

    # AV-aware fallback: if file physically exists, accept it
    $expectedExe = Join-Path $targetDir 'python.exe'
    if (Test-Path $expectedExe) {
        Write-Host ''
        Write-Host "    python.exe exists at: $expectedExe"
        Write-Host '    But execution is being blocked - likely Antivirus scanning new file.'
        Write-Host '    Waiting 15 seconds for AV scan to finish, then retrying...'
        Start-Sleep -Seconds 15

        $ver = Test-PythonExe $expectedExe
        if ($ver) {
            Write-Host "    OK after AV wait: Python $ver"
            return @{ Cmd = "`"$expectedExe`""; Version = $ver; ExePath = $expectedExe }
        }

        # Even more permissive
        $ver = Test-PythonExe -exePath $expectedExe -Permissive
        if ($ver) {
            Write-Host "    Accepting Python at $expectedExe based on file presence."
            Write-Host "    NOTE: If next steps fail, exclude this folder from your antivirus."
            return @{ Cmd = "`"$expectedExe`""; Version = '3.12.7 (assumed)'; ExePath = $expectedExe }
        }
    }

    Write-Host '    [ERROR] Installer reported success but Python cannot be detected.'
    Write-Host '    Diagnostics:'
    Write-Host "      Target dir exists: $(Test-Path $targetDir)"
    if (Test-Path $targetDir) {
        $items = Get-ChildItem $targetDir -ErrorAction SilentlyContinue | Select-Object -First 10 Name
        Write-Host "      Contents: $($items.Name -join ', ')"
    }
    Write-Host ''
    Write-Host '    LIKELY CAUSE: Antivirus is blocking python.exe execution.'
    Write-Host '    Please:'
    Write-Host "      1. Open Windows Defender / your AV settings"
    Write-Host "      2. Add this folder to exclusions: $targetDir"
    Write-Host "      3. Run install.bat again"
    return $null
}

# ============ main ============
Write-Host '    Searching for installed Python...'
$found = Find-Python
if ($found) {
    Write-Host "    Found Python $($found.Version)"
    if ($found.ExePath) { Write-Host "    Path: $($found.ExePath)" }
    Set-Content -Path $OutFile -Value $found.Cmd -Encoding ASCII -NoNewline
    exit 0
}

Write-Host '    Python 3.10+ not found in any known location.'
$installed = Install-Python
if (-not $installed) {
    Write-Host ''
    Write-Host '    Final retry after 5 seconds...'
    Start-Sleep -Seconds 5
    $found = Find-Python
    if ($found) {
        Write-Host "    Found Python $($found.Version) on final retry!"
        Set-Content -Path $OutFile -Value $found.Cmd -Encoding ASCII -NoNewline
        exit 0
    }
    Write-Host ''
    Write-Host '    Python install failed.'
    exit 1
}

Write-Host "    OK: Python $($installed.Version) ready"
if ($installed.ExePath) { Write-Host "    Path: $($installed.ExePath)" }
Set-Content -Path $OutFile -Value $installed.Cmd -Encoding ASCII -NoNewline
exit 0
