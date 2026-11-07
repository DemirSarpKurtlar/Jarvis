<#
.SYNOPSIS
    JARVIS'i Windows'ta baştan sona derler: testler, PyInstaller, duman testi, Inno Setup kurulumu.

.EXAMPLE
    # Depo kökünde, sanal ortam etkinken:
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Install -SkipTests

.NOTES
    Çıktılar:
      dist\JARVIS\JARVIS.exe                         PyInstaller klasörü
      dist\installer\JARVIS-Kurulum-<sürüm>.exe      kurulum programı
    Inno Setup 6 gerekir: https://jrsoftware.org/isdl.php  (ya da: choco install innosetup -y)
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$Install,          # önce requirements-dev.txt ve packaging\requirements-build.txt kur
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [double]$SmokeSeconds = 5
)

$ErrorActionPreference = "Stop"
# Not: Bu dosya UTF-8 BOM ile kaydedilmeli (Windows PowerShell 5.1 Türkçe karakterleri doğru okusun)
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step([string]$Name) { Write-Host "`n==> $Name" -ForegroundColor Cyan }
function Check([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What başarısız (çıkış kodu $LASTEXITCODE)" }
}

if ($Install) {
    Step "Bağımlılıklar kuruluyor"
    & $Python -m pip install --upgrade pip; Check "pip güncelleme"
    & $Python -m pip install -r requirements-dev.txt -r packaging\requirements-build.txt; Check "pip install"
}

$Version = (& $Python scripts\version_info.py).Trim(); Check "sürüm okuma"
$Numeric = (& $Python scripts\version_info.py --numeric).Trim(); Check "sürüm okuma"
Write-Host "JARVIS sürümü: $Version ($Numeric)"

if (-not $SkipTests) {
    Step "Testler (pytest)"
    $env:QT_QPA_PLATFORM = "offscreen"
    & $Python -m pytest -q; Check "pytest"
    Remove-Item Env:\QT_QPA_PLATFORM
}

Step "PyInstaller"
& $Python -m PyInstaller packaging\jarvis.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
Check "PyInstaller"
$Exe = Join-Path $Root "dist\JARVIS\JARVIS.exe"
if (-not (Test-Path $Exe)) { throw "Beklenen exe yok: $Exe" }

Step "Paketlenmiş exe duman testi ($SmokeSeconds sn)"
$SmokeHome = Join-Path $Root "build\smoke-home"
if (Test-Path $SmokeHome) { Remove-Item -Recurse -Force $SmokeHome }
$env:JARVIS_HOME = $SmokeHome
& $Python scripts\smoke_run.py --log (Join-Path $SmokeHome "logs\jarvis.log") `
    --expect "Anahtar deposu: Windows Credential Manager (güvenli)" -- $Exe --smoke-test $SmokeSeconds
$smokeExit = $LASTEXITCODE
Remove-Item Env:\JARVIS_HOME
if ($smokeExit -ne 0) { throw "Duman testi başarısız" }

if ($SkipInstaller) { Write-Host "`nKurulum programı atlandı. Çıktı: $Exe" -ForegroundColor Green; exit 0 }

Step "Inno Setup"
$Iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    $Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "ISCC.exe bulunamadı. Inno Setup 6 kurun: choco install innosetup -y" }
& $Iscc "/DAppVersion=$Version" "/DAppNumericVersion=$Numeric" `
    "/DSourceDir=$Root\dist\JARVIS" "/DOutputDir=$Root\dist\installer" packaging\jarvis.iss
Check "Inno Setup derlemesi"

$Setup = Join-Path $Root "dist\installer\JARVIS-Kurulum-$Version.exe"
Write-Host "`nHazır:" -ForegroundColor Green
Write-Host "  $Exe"
Write-Host "  $Setup"
