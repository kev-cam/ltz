# bootstrap.ps1 -- fresh Windows box -> WSL Ubuntu + all deps + the /opt/sims funnel.
#
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1
#   ... -Distro Ubuntu-26.04 -User claude          # pick distro / work user
#   ... -WithMingw                                 # also install mingw (rebuild the shim)
#   ... -ArtifactsDir D:\sims-artifacts            # restore engines+OpenVAF+PDK+ssh from pack-artifacts.sh
#   ... -DeployShim                                # also swap the QSPICE engine for the shim
#
# Installing WSL/a distro needs an elevated shell (and possibly one reboot).
# Provisioning runs as root inside WSL, so no interactive distro OOBE is needed.
param(
  [string]$Distro = "Ubuntu",
  [string]$User = "claude",
  [string]$SrcRoot = "C:\cygwin64\usr\local\src",
  [switch]$WithMingw,
  [switch]$DeployShim,
  [switch]$Repair,
  [string]$Extras = "",
  [string]$ArtifactsDir = "C:\cygwin64\home\Claude\sims-artifacts"
)
$ErrorActionPreference = "Stop"

function ConvertTo-WslPath([string]$WinPath) {
  $p = $WinPath -replace '\\','/'
  if ($p -match '^([A-Za-z]):(.*)$') { return '/mnt/' + $matches[1].ToLower() + $matches[2] }
  return $p
}
function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
           [Security.Principal.WindowsBuiltinRole]::Administrator)
}
function Test-WslReady {
  try { $null = & wsl.exe --status 2>$null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}
function Get-WslDistros {
  $raw = & wsl.exe -l -q 2>$null
  if (-not $raw) { return @() }
  return ($raw | ForEach-Object { ($_ -replace "`0","").Trim() } | Where-Object { $_ })
}

Write-Host "== qshim /opt/sims bootstrap =="

# --- 1. WSL platform ---------------------------------------------------------
if (-not (Test-WslReady)) {
  if (-not (Test-Admin)) { throw "WSL is not enabled and this shell is not elevated. Re-run as Administrator." }
  Write-Host "Enabling the WSL platform (no distro yet)..."
  & wsl.exe --install --no-distribution
  Write-Warning "Reboot to finish enabling WSL, then re-run this script."
  exit 0
}

# --- 2. Distro ---------------------------------------------------------------
$distros = Get-WslDistros
if ($distros -notcontains $Distro) {
  # reuse an already-installed Ubuntu* rather than forcing an exact version name
  $existingUbuntu = $distros | Where-Object { $_ -like 'Ubuntu*' } | Select-Object -First 1
  if ($existingUbuntu) {
    Write-Host "Distro '$Distro' not registered; using existing '$existingUbuntu'."
    $Distro = $existingUbuntu
  }
}
if ($distros -notcontains $Distro) {
  if (-not (Test-Admin)) { throw "Distro '$Distro' is not installed and this shell is not elevated. Re-run as Administrator." }
  $online = (& wsl.exe -l -o 2>$null | ForEach-Object { ($_ -replace "`0","").Trim() })
  $target = $Distro
  if (($online -join "`n") -notmatch [regex]::Escape($Distro)) {
    Write-Warning "'$Distro' not in 'wsl -l -o'; falling back to 'Ubuntu' (latest)."
    $target = "Ubuntu"
  }
  Write-Host "Installing distro $target ..."
  & wsl.exe --install -d $target --no-launch
  if ($LASTEXITCODE -ne 0) { throw "wsl --install -d $target failed ($LASTEXITCODE)" }
  # nudge registration so root is usable without the interactive OOBE
  & wsl.exe -d $target -u root -- true 2>$null
  $Distro = $target
}

# --- Repair mode: just run the doctor + targeted self-heal, then exit --------
if ($Repair) {
  $art = ""; if ($ArtifactsDir) { $art = ConvertTo-WslPath $ArtifactsDir }
  Write-Host "Repair mode: sims doctor + repair in '$Distro'..."
  & wsl.exe -d $Distro -u root -- env "SIMS_ARTIFACTS=$art" /opt/sims/sims repair
  exit $LASTEXITCODE
}

# --- 3. Provision inside WSL (apt + user + /opt/sims), as root ---------------
$wslSrc  = ConvertTo-WslPath $SrcRoot
$bootWsl = "$wslSrc/ltz/sims/bootstrap-wsl.sh"
$mingwArg = '0'; if ($WithMingw) { $mingwArg = '1' }
$artArg = ""; if ($ArtifactsDir) { $artArg = ConvertTo-WslPath $ArtifactsDir }
$wslArgs = @($bootWsl, $User, $mingwArg, $artArg)
if ($Extras) { $wslArgs += $Extras }

Write-Host "Provisioning '$Distro' (apt deps, work user, /opt/sims)..."
& wsl.exe -d $Distro -u root -- bash @wslArgs
if ($LASTEXITCODE -ne 0) { throw "WSL provisioning failed (exit $LASTEXITCODE)" }

# --- 4. Optionally deploy the qshim (swaps the QSPICE engine exe) -------------
if ($DeployShim) {
  $installPs1 = Join-Path $SrcRoot 'ltz\qshim\install.ps1'
  if (Test-Path $installPs1) {
    Write-Host "Deploying qshim (requires QSPICE installed)..."
    & powershell.exe -ExecutionPolicy Bypass -File $installPs1
  } else { Write-Warning "qshim installer not found at $installPs1" }
}

Write-Host "`n== bootstrap complete =="
Write-Host "Pick the GUI engine with: %LOCALAPPDATA%\qspice-shim\mode.txt  (passthru|xyce|xyce-mpi|ngspice)"
Write-Host "Run an engine directly in WSL:  /opt/sims/sim <engine> <args>   (sim --list to enumerate)"
