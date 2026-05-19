<#
.SYNOPSIS
    CadSllmAgent 플러그인을 .bundle 형태로 빌드·패키징하는 PowerShell 스크립트.

.DESCRIPTION
    1. CadSllmAgent (DLL 플러그인) 빌드
    2. CadSllmAgent.Updater (updater.exe) 퍼블리시
    3. .bundle/Contents/ 폴더에 필요 파일 복사
    4. version.txt 생성
    5. CadSllmAgent_v{version}.zip 으로 패키징

.PARAMETER Version
    배포할 버전 문자열 (예: 1.0.0). 기본값: version.txt 내용.

.EXAMPLE
    .\scripts\build_bundle.ps1 -Version 1.1.0
#>

param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# ── 경로 설정 ──────────────────────────────────────────────────────────
$PluginProject   = Join-Path $RepoRoot "CadSllmAgent\CadSllmAgent.csproj"
$UpdaterProject  = Join-Path $RepoRoot "CadSllmAgent.Updater\CadSllmAgent.Updater.csproj"
$InstallerProject = Join-Path $RepoRoot "CadSllmAgent.Installer\CadSllmAgent.Installer.csproj"
$BundleTemplate  = Join-Path $RepoRoot "CadSllmAgent.Bundle"
$BuildOutput     = Join-Path $RepoRoot "build_output"
$BundleOutput    = Join-Path $BuildOutput "CadSllmAgent.bundle"
$ContentsDir     = Join-Path $BundleOutput "Contents"
$PluginBuildDir  = Join-Path $RepoRoot "CadSllmAgent\bin\Release\net10.0-windows"
$UpdaterPubDir   = Join-Path $BuildOutput "updater_publish"
$InstallerPubDir = Join-Path $BuildOutput "installer_publish"

function Get-MSBuildPath {
    $candidates = @(
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\amd64\MSBuild.exe",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Insiders\MSBuild\Current\Bin\MSBuild.exe",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Insiders\MSBuild\Current\Bin\amd64\MSBuild.exe",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    $command = Get-Command MSBuild.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    return $null
}

# ── 버전 결정 ──────────────────────────────────────────────────────────
if (-not $Version) {
    $versionFile = Join-Path $BundleTemplate "Contents\version.txt"
    if (Test-Path $versionFile) {
        $Version = (Get-Content $versionFile -Raw).Trim()
    }
    else {
        $Version = "1.0.0"
    }
}

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  CadSllmAgent Bundle Builder — v$Version"
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

# ── 1. 이전 빌드 정리 ─────────────────────────────────────────────────
Write-Host "[1/7] 이전 빌드 정리..." -ForegroundColor Yellow
if (Test-Path $BuildOutput) {
    Remove-Item -Recurse -Force $BuildOutput
}
New-Item -ItemType Directory -Path $ContentsDir -Force | Out-Null

# ── 2. 플러그인 DLL 빌드 ──────────────────────────────────────────────
Write-Host "[2/7] CadSllmAgent 빌드 (Release)..." -ForegroundColor Yellow
$MSBuildPath = Get-MSBuildPath
if ($MSBuildPath) {
    # Platform="Any CPU" 명시 시 산출물이 bin\Any CPU\Release\ 로 들어가 PluginBuildDir 와 mismatch.
    # Platform 미지정 시 default(AnyCPU) 사용하고 bin\Release\ 에 산출 → 스크립트 하단 경로와 일치.
    & $MSBuildPath $PluginProject /p:Configuration=Release /nologo /verbosity:quiet
}
else {
    Write-Host "  ⚠️ MSBuild.exe를 찾지 못해 dotnet build로 시도합니다." -ForegroundColor Yellow
    dotnet build $PluginProject -c Release --nologo -v q
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 플러그인 빌드 실패!" -ForegroundColor Red
    exit 1
}
Write-Host "  ✅ 빌드 완료." -ForegroundColor Green

# ── 3. Updater 퍼블리시 ───────────────────────────────────────────────
Write-Host "[3/7] updater.exe 퍼블리시..." -ForegroundColor Yellow
dotnet publish $UpdaterProject -c Release -o $UpdaterPubDir --nologo -v q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ Updater 퍼블리시 실패!" -ForegroundColor Red
    exit 1
}
Write-Host "  ✅ 퍼블리시 완료." -ForegroundColor Green

# -- 4. Installer publish -----------------------------------------------------
Write-Host "[4/7] installer.exe publish..." -ForegroundColor Yellow
dotnet publish $InstallerProject -c Release -o $InstallerPubDir --nologo -v q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installer publish failed!" -ForegroundColor Red
    exit 1
}
$installerExe = Join-Path $InstallerPubDir "CadSllmAgent.Installer.exe"
if (-not (Test-Path $installerExe)) {
    Write-Host "  installer.exe not found: $installerExe" -ForegroundColor Red
    exit 1
}
Write-Host "  installer.exe publish complete." -ForegroundColor Green
# ── 4. Contents 폴더 조립 ─────────────────────────────────────────────
Write-Host "[5/7] Contents 폴더 조립..." -ForegroundColor Yellow

# 4a. 플러그인 DLL + 의존성 복사
$pluginFiles = Get-ChildItem -Path $PluginBuildDir -File -Recurse | Where-Object {
    $_.Extension -in @(".dll", ".json", ".xml", ".pdb")
}
foreach ($f in $pluginFiles) {
    $relativePath = $f.FullName.Substring($PluginBuildDir.Length + 1)
    $destPath = Join-Path $ContentsDir $relativePath
    $destDir = Split-Path $destPath -Parent
    if (-not (Test-Path $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }
    Copy-Item $f.FullName -Destination $destPath -Force
}

# WebView2Loader.dll 도 포함 (runtimes 하위)
$wv2Loader = Join-Path $PluginBuildDir "WebView2Loader.dll"
if (Test-Path $wv2Loader) {
    Copy-Item $wv2Loader -Destination (Join-Path $ContentsDir "WebView2Loader.dll") -Force
}

# 4b. updater.exe 복사
$updaterExe = Join-Path $UpdaterPubDir "updater.exe"
if (Test-Path $updaterExe) {
    Copy-Item $updaterExe -Destination (Join-Path $ContentsDir "updater.exe") -Force
    Write-Host "  updater.exe 포함됨."
}
else {
    Write-Host "  ⚠️ updater.exe를 찾을 수 없습니다: $updaterExe" -ForegroundColor Yellow
}

# 4c. version.txt 생성
Set-Content -Path (Join-Path $ContentsDir "version.txt") -Value $Version -NoNewline
Write-Host "  version.txt: $Version"

$fileCount = (Get-ChildItem $ContentsDir -Recurse -File).Count
Write-Host "  ✅ $fileCount 개 파일 조립 완료." -ForegroundColor Green

# ── 5. PackageContents.xml 복사 & 버전 갱신 ───────────────────────────
Write-Host "[6/7] PackageContents.xml 생성..." -ForegroundColor Yellow
$xmlTemplate = Join-Path $BundleTemplate "PackageContents.xml"
$xmlDest = Join-Path $BundleOutput "PackageContents.xml"

if (Test-Path $xmlTemplate) {
    $xmlContent = Get-Content $xmlTemplate -Raw -Encoding UTF8
    # 버전 치환
    $xmlContent = $xmlContent -replace 'AppVersion="[^"]*"', "AppVersion=`"$Version`""
    $xmlContent = $xmlContent -replace 'Version="[^"]*"', "Version=`"$Version`""
    Set-Content -Path $xmlDest -Value $xmlContent -Encoding UTF8
}
else {
    Write-Host "  ⚠️ 템플릿 없음. 인라인 생성." -ForegroundColor Yellow
    @"
<?xml version="1.0" encoding="utf-8"?>
<ApplicationPackage SchemaVersion="1.0" AppVersion="$Version"
    ProductCode="{E3FAF42F-796B-4C81-9D03-C93ECE8EDD19}"
    Name="CadSllmAgent" Description="AI-powered CAD Review Agent" Author="SKN23-2TEAM">
  <CompanyDetails Name="SKN23-2TEAM" />
  <Components>
    <RuntimeRequirements OS="Win64" Platform="AutoCAD" />
    <ComponentEntry AppName="CadSllmAgent" Version="$Version"
        ModuleName="./Contents/CadSllmAgent.dll" AppType="Assembly"
        LoadOnAutoCADStartup="True" />
  </Components>
</ApplicationPackage>
"@ | Set-Content -Path $xmlDest -Encoding UTF8
}
Write-Host "  ✅ PackageContents.xml 생성 완료." -ForegroundColor Green

# ── 6. ZIP 패키징 ─────────────────────────────────────────────────────
Write-Host "[7/7] ZIP 패키징..." -ForegroundColor Yellow
$zipName = "CadSllmAgent_v$Version.zip"
$zipPath = Join-Path $BuildOutput $zipName

# Compress-Archive는 폴더 자체가 아닌 Contents 내부만 포함
# (Installer/Updater가 Contents/ 에 직접 해제하므로)
Compress-Archive -Path (Join-Path $ContentsDir "*") -DestinationPath $zipPath -Force

$zipSize = (Get-Item $zipPath).Length
$zipSizeMB = [math]::Round($zipSize / 1MB, 2)

Write-Host "  ✅ $zipName ($zipSizeMB MB)" -ForegroundColor Green

# ── 완료 ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  빌드 완료!"
Write-Host "  Bundle: $BundleOutput"
Write-Host "  ZIP:       $zipPath"
Write-Host "  Installer: $installerExe"
Write-Host ""
Write-Host "  S3 배포 명령:"
Write-Host "    python scripts/deploy_plugin.py --version $Version --zip `"$zipPath`" --installer `"$installerExe`""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""
