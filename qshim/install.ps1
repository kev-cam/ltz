# install.ps1 — swap the QSPICE engine exes for the Xyce shim. RUN ELEVATED.
#   powershell -ExecutionPolicy Bypass -File install.ps1            # install
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Restore   # uninstall
# Mode control afterwards (no elevation needed):
#   echo xyce     > $env:LOCALAPPDATA\qspice-shim\mode.txt   # GUI sims run on Xyce
#   echo passthru > $env:LOCALAPPDATA\qspice-shim\mode.txt   # real QSPICE engine
param([switch]$Restore)

$qdir = 'C:\Program Files\QSPICE'
$shim = Join-Path $PSScriptRoot 'qspice-shim.exe'
$engines = @('QSPICE64.exe', 'QSPICE80.exe')

if (-not (Test-Path $qdir)) { Write-Error "QSPICE not found at $qdir"; exit 1 }

if ($Restore) {
    foreach ($e in $engines) {
        $real = Join-Path $qdir ($e -replace '\.exe$', '.real.exe')
        $cur  = Join-Path $qdir $e
        if (Test-Path $real) {
            Remove-Item $cur -Force -Confirm:$false -ErrorAction SilentlyContinue
            Move-Item $real $cur -Force
            Write-Host "restored $e"
        } else { Write-Host "$e already original (no .real.exe)" }
    }
    exit 0
}

if (-not (Test-Path $shim)) { Write-Error "build qspice-shim.exe first"; exit 1 }

foreach ($e in $engines) {
    $cur  = Join-Path $qdir $e
    $real = Join-Path $qdir ($e -replace '\.exe$', '.real.exe')
    if (-not (Test-Path $cur)) { Write-Host "skip $e (not present)"; continue }
    if (Test-Path $real) { Write-Host "skip $e (shim already installed)"; continue }
    Move-Item $cur $real
    Copy-Item $shim $cur
    Write-Host "shimmed $e (real engine -> $(Split-Path $real -Leaf))"
}

$cfg = Join-Path $env:LOCALAPPDATA 'qspice-shim'
New-Item -ItemType Directory -Force $cfg | Out-Null
if (-not (Test-Path "$cfg\mode.txt")) { Set-Content "$cfg\mode.txt" 'passthru' -Encoding ascii }
Write-Host "installed. mode=$(Get-Content "$cfg\mode.txt") (edit $cfg\mode.txt; log: $cfg\shim.log)"
