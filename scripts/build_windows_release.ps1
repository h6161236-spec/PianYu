param(
    [string]$PythonExe = "python",
    [string]$FfmpegDir = "",
    [ValidateSet("major", "feature", "bugfix", "none")]
    [string]$ReleaseKind = "feature",
    [switch]$SkipZip,
    [switch]$SkipBundledAsrModel
)

$ErrorActionPreference = "Stop"

$AppBundleName = [string]::Concat([char]0x7247, [char]0x8BED)
$AppExecutableName = "$AppBundleName.exe"
$ReleaseArchivePrefix = "$AppBundleName-windows-v"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$pyprojectText = Get-Content (Join-Path $RepoRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($pyprojectText, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "Could not read the project version from pyproject.toml."
}
$packageVersion = $versionMatch.Groups[1].Value

$releaseStatePath = Join-Path $RepoRoot "packaging\release_version.json"
if (-not (Test-Path $releaseStatePath)) {
    throw "Could not find release version state: $releaseStatePath"
}
$releaseState = Get-Content $releaseStatePath -Raw | ConvertFrom-Json

function Get-ReleaseVersionStateValue {
    param(
        [object]$State,
        [string]$Name,
        [int]$DefaultValue = 0
    )

    if ($null -eq $State) {
        return $DefaultValue
    }
    $property = $State.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $DefaultValue
    }
    return [int]$property.Value
}

function Get-ReleaseVersionPlan {
    param(
        [object]$State,
        [string]$Kind
    )

    $major = Get-ReleaseVersionStateValue -State $State -Name "major" -DefaultValue 1
    $feature = Get-ReleaseVersionStateValue -State $State -Name "feature" -DefaultValue 0
    $bugfix = Get-ReleaseVersionStateValue -State $State -Name "bugfix" -DefaultValue 0
    $lastVersionProperty = $State.PSObject.Properties["last_version"]
    $lastVersion = if ($lastVersionProperty) { [string]$lastVersionProperty.Value } else { "$major" }

    switch ($Kind) {
        "major" {
            $major += 1
            $feature = 0
            $bugfix = 0
            $version = "$major"
        }
        "feature" {
            $feature += 1
            $version = "$major.$feature"
        }
        "bugfix" {
            $bugfix += 1
            $version = "$major." + ("{0:00}" -f $bugfix)
        }
        "none" {
            $version = $lastVersion
        }
        default {
            throw "Unsupported release kind: $Kind"
        }
    }

    return [PSCustomObject]@{
        Major       = $major
        Feature     = $feature
        Bugfix      = $bugfix
        Version     = $version
        Previous    = $lastVersion
        ReleaseKind = $Kind
    }
}

function Save-ReleaseVersionPlan {
    param(
        [string]$StatePath,
        [object]$Plan
    )

    $payload = [ordered]@{
        major        = [int]$Plan.Major
        feature      = [int]$Plan.Feature
        bugfix       = [int]$Plan.Bugfix
        last_version = [string]$Plan.Version
    }
    $json = $payload | ConvertTo-Json -Depth 4
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($StatePath, $json, $utf8)
}

$releasePlan = Get-ReleaseVersionPlan -State $releaseState -Kind $ReleaseKind
$releaseVersion = [string]$releasePlan.Version

$autoDetectedFfmpegDir = $false
$resolvedStandaloneFfprobePath = ""

function Get-AppIconSourcePath {
    $preferredNames = @(
        "app_icon.ico",
        "app_icon.png"
    )
    foreach ($name in $preferredNames) {
        $candidate = Join-Path $RepoRoot $name
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    $iconCandidate = Get-ChildItem $RepoRoot -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".ico", ".png") } |
        Sort-Object Length -Descending |
        Select-Object -First 1
    if ($iconCandidate) {
        return $iconCandidate.FullName
    }
    return $null
}

function Prepare-AppIconAssets {
    $iconSource = Get-AppIconSourcePath
    if (-not $iconSource) {
        Remove-Item Env:VCUT_APP_ICON -ErrorAction SilentlyContinue
        Remove-Item Env:VCUT_RUNTIME_ICON -ErrorAction SilentlyContinue
        Write-Host "No application icon file was found. Continuing without custom icon."
        return
    }

    $releaseAssetsDir = Join-Path $RepoRoot "build\release_assets"
    New-Item -ItemType Directory -Force -Path $releaseAssetsDir | Out-Null
    $runtimeIconPath = Join-Path $releaseAssetsDir "app_icon.ico"

    if ([System.IO.Path]::GetExtension($iconSource).ToLowerInvariant() -eq ".ico") {
        Copy-Item $iconSource $runtimeIconPath -Force
    }
    else {
        $iconScript = @'
from pathlib import Path
import sys
from PySide6.QtGui import QImage

source = Path(sys.argv[1])
target = Path(sys.argv[2])
image = QImage(str(source))
if image.isNull():
    raise SystemExit(f"Could not load icon source: {source}")
target.parent.mkdir(parents=True, exist_ok=True)
if not image.save(str(target), "ICO"):
    raise SystemExit(f"Could not save icon file: {target}")
'@
        $iconScript | & $PythonExe - $iconSource $runtimeIconPath
        if ($LASTEXITCODE -ne 0) {
            throw "Could not convert the application icon to ICO."
        }
    }

    $env:VCUT_APP_ICON = $runtimeIconPath
    $env:VCUT_RUNTIME_ICON = $runtimeIconPath
    Write-Host "Using application icon: $iconSource"
}

function Get-AppSettingsPath {
    if (-not $env:APPDATA) {
        return $null
    }
    $settingsPath = Join-Path $env:APPDATA "VCutStudio\settings.json"
    if (Test-Path $settingsPath) {
        return $settingsPath
    }
    return $null
}

function Resolve-AsrSnapshotDir {
    param(
        [string]$CacheDir,
        [string]$ModelName
    )

    if (-not $CacheDir -or -not $ModelName) {
        return $null
    }

    $repoDir = Join-Path $CacheDir ("models--Systran--faster-whisper-" + $ModelName)
    if (-not (Test-Path $repoDir)) {
        return $null
    }

    $refsMainPath = Join-Path $repoDir "refs\main"
    if (Test-Path $refsMainPath) {
        $revision = (Get-Content $refsMainPath -Raw).Trim()
        if ($revision) {
            $snapshotPath = Join-Path $repoDir (Join-Path "snapshots" $revision)
            if (Test-Path $snapshotPath) {
                return (Resolve-Path $snapshotPath).Path
            }
        }
    }

    $snapshotDir = Get-ChildItem (Join-Path $repoDir "snapshots") -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($snapshotDir) {
        return $snapshotDir.FullName
    }
    return $null
}

function Get-BundledAsrModelPlan {
    $settingsPath = Get-AppSettingsPath
    if (-not $settingsPath) {
        return $null
    }

    $settingsJson = [System.IO.File]::ReadAllText($settingsPath)
    $settings = $settingsJson | ConvertFrom-Json
    $asr = $settings.asr
    if (-not $asr) {
        return $null
    }

    $modelName = [string]$asr.model_name
    $sourcePath = $null
    $localModelDir = [string]$asr.local_model_dir
    if ($localModelDir -and (Test-Path $localModelDir)) {
        $sourcePath = (Resolve-Path $localModelDir).Path
        if (-not $modelName) {
            $modelName = Split-Path $sourcePath -Leaf
        }
    }

    if (-not $sourcePath) {
        $sourcePath = Resolve-AsrSnapshotDir -CacheDir ([string]$asr.model_cache_dir) -ModelName $modelName
    }

    if (-not $sourcePath) {
        return $null
    }

    return [PSCustomObject]@{
        SourcePath   = $sourcePath
        ModelName    = $modelName
        Settings     = $settings
        SettingsPath = $settingsPath
    }
}

function Get-BundledTtsPlan {
    $ttsRoot = Join-Path $RepoRoot "models\tts"
    if (-not (Test-Path $ttsRoot)) {
        return $null
    }

    $kokoroModel = Join-Path $ttsRoot "kokoro-en-v0_19\model.onnx"
    $kokoroVoices = Join-Path $ttsRoot "kokoro-en-v0_19\voices.bin"
    $sherpaExe = $null
    foreach ($directory in (Get-ChildItem (Join-Path $ttsRoot "sherpa-onnx-*") -Directory -ErrorAction SilentlyContinue)) {
        $candidate = Join-Path $directory.FullName "bin\sherpa-onnx-offline-tts.exe"
        if (Test-Path $candidate) {
            $sherpaExe = $candidate
            break
        }
    }

    if ((Test-Path $kokoroModel) -and (Test-Path $kokoroVoices) -and $sherpaExe) {
        return [PSCustomObject]@{
            RootPath = (Resolve-Path $ttsRoot).Path
        }
    }

    return $null
}

function Write-PortableSettings {
    param(
        [string]$PortableSettingsPath,
        [object]$CurrentSettings,
        [string]$RelativeModelDir,
        [switch]$ForceLocalKokoroTts
    )

    $portableSettings = [ordered]@{}

    if ($CurrentSettings.asr) {
        $resolvedAsrModelDir = if ($RelativeModelDir) { $RelativeModelDir } else { "" }
        $portableSettings["asr"] = [ordered]@{
            model_name        = [string]$CurrentSettings.asr.model_name
            device            = [string]$CurrentSettings.asr.device
            language          = [string]$CurrentSettings.asr.language
            vad_enabled       = [bool]$CurrentSettings.asr.vad_enabled
            segment_max_seconds = [int]$CurrentSettings.asr.segment_max_seconds
            min_confidence    = [double]$CurrentSettings.asr.min_confidence
            model_cache_dir   = ""
            local_model_dir   = $resolvedAsrModelDir
            local_files_only  = if ($RelativeModelDir) { $true } else { $false }
        }
    }

    if ($CurrentSettings.translation) {
        $portableSettings["translation"] = [ordered]@{
            provider_type = [string]$CurrentSettings.translation.provider_type
            base_url      = ""
            api_key       = ""
            model         = ""
            system_prompt = [string]$CurrentSettings.translation.system_prompt
            timeout_sec   = [int]$CurrentSettings.translation.timeout_sec
            max_retries   = [int]$CurrentSettings.translation.max_retries
            concurrency   = [int]$CurrentSettings.translation.concurrency
            batch_size    = [int]$CurrentSettings.translation.batch_size
            proxy_enabled = [bool]$CurrentSettings.translation.proxy_enabled
            http_proxy    = [string]$CurrentSettings.translation.http_proxy
            https_proxy   = [string]$CurrentSettings.translation.https_proxy
            no_proxy      = [string]$CurrentSettings.translation.no_proxy
        }
    }

    $bundledTtsRoot = Join-Path $RepoRoot "models\tts"
    $hasBundledMelo = Test-Path (Join-Path $bundledTtsRoot "vits-melo-tts-zh_en\model.onnx")
    $englishProviderType = if ($ForceLocalKokoroTts) { "kokoro_local" } elseif ($CurrentSettings.tts) { [string]$CurrentSettings.tts.english_provider_type } else { "builtin_voice_catalog" }
    if (-not $englishProviderType) {
        $englishProviderType = if ($ForceLocalKokoroTts) { "kokoro_local" } elseif ($CurrentSettings.tts) { [string]$CurrentSettings.tts.provider_type } else { "builtin_voice_catalog" }
    }
    $englishDefaultVoice = if ($ForceLocalKokoroTts) { "af_bella" } elseif ($CurrentSettings.tts) { [string]$CurrentSettings.tts.english_default_voice } else { "emma_clear" }
    if (-not $englishDefaultVoice) {
        $englishDefaultVoice = if ($ForceLocalKokoroTts) { "af_bella" } elseif ($CurrentSettings.tts) { [string]$CurrentSettings.tts.default_voice } else { "emma_clear" }
    }
    $chineseProviderType = if ($hasBundledMelo) { "melo_local" } elseif ($CurrentSettings.tts) { [string]$CurrentSettings.tts.chinese_provider_type } else { "builtin_voice_catalog" }
    if (-not $chineseProviderType) {
        $chineseProviderType = if ($hasBundledMelo) { "melo_local" } else { "builtin_voice_catalog" }
    }
    $chineseDefaultVoice = if ($hasBundledMelo) { "melo_zh_female" } elseif ($CurrentSettings.tts) { [string]$CurrentSettings.tts.chinese_default_voice } else { "" }
    if (-not $chineseDefaultVoice -and $hasBundledMelo) {
        $chineseDefaultVoice = "melo_zh_female"
    }
    $portableSettings["tts"] = [ordered]@{
        provider_type          = $englishProviderType
        default_voice          = $englishDefaultVoice
        rate                   = if ($CurrentSettings.tts) { [double]$CurrentSettings.tts.rate } else { 1.0 }
        volume                 = if ($CurrentSettings.tts) { [double]$CurrentSettings.tts.volume } else { 1.0 }
        sample_rate            = if ($CurrentSettings.tts) { [int]$CurrentSettings.tts.sample_rate } else { 22050 }
        retry_count            = if ($CurrentSettings.tts) { [int]$CurrentSettings.tts.retry_count } else { 2 }
        preview_text           = if ($CurrentSettings.tts) { [string]$CurrentSettings.tts.preview_text } else { "This is a sample English dubbing preview." }
        english_provider_type  = $englishProviderType
        english_default_voice  = $englishDefaultVoice
        english_preview_text   = if ($CurrentSettings.tts) { [string]$CurrentSettings.tts.english_preview_text } else { "This is a sample English dubbing preview." }
        chinese_provider_type  = $chineseProviderType
        chinese_default_voice  = $chineseDefaultVoice
        chinese_preview_text   = if ($CurrentSettings.tts) { [string]$CurrentSettings.tts.chinese_preview_text } else { [string]::Concat([char]0x8FD9, [char]0x662F, [char]0x4E00, [char]0x6BB5, [char]0x4E2D, [char]0x6587, [char]0x914D, [char]0x97F3, [char]0x8BD5, [char]0x542C, [char]0x6587, [char]0x672C, [char]0x3002) }
    }

    $json = $portableSettings | ConvertTo-Json -Depth 6
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($PortableSettingsPath, $json, $utf8)
}

& $PythonExe -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Run: pip install -e .[release]"
}

Prepare-AppIconAssets

if (-not $FfmpegDir) {
    $ffmpegSource = Get-Command ffmpeg -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    if ($ffmpegSource) {
        $FfmpegDir = Split-Path -Parent $ffmpegSource
        $autoDetectedFfmpegDir = $true
    }
}

if ($FfmpegDir) {
    $resolvedFfmpegDir = (Resolve-Path $FfmpegDir).Path
    $hasFfmpeg = Test-Path (Join-Path $resolvedFfmpegDir "ffmpeg.exe")
    $hasFfprobe = Test-Path (Join-Path $resolvedFfmpegDir "ffprobe.exe")

    if (-not $hasFfmpeg) {
        if ($autoDetectedFfmpegDir) {
            Remove-Item Env:VCUT_BUNDLE_FFMPEG_DIR -ErrorAction SilentlyContinue
            Write-Host "ffmpeg was detected automatically, but ffmpeg.exe was not found in: $resolvedFfmpegDir"
            Write-Host "Continuing without bundled ffmpeg binaries."
        }
        else {
            throw "Missing ffmpeg.exe in ffmpeg directory: $resolvedFfmpegDir"
        }
    }
    else {
        $env:VCUT_BUNDLE_FFMPEG_DIR = $resolvedFfmpegDir
        Write-Host "Bundling ffmpeg from: $resolvedFfmpegDir"
        if (-not $hasFfprobe) {
            $ffprobeSource = Get-Command ffprobe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
            if ($ffprobeSource) {
                $resolvedStandaloneFfprobePath = (Resolve-Path $ffprobeSource).Path
                Write-Host "Bundling ffprobe from: $resolvedStandaloneFfprobePath"
            }
            else {
                Write-Host "ffprobe.exe was not found. The packaged app will fall back to ffmpeg-based probing."
            }
        }
    }
}
else {
    Remove-Item Env:VCUT_BUNDLE_FFMPEG_DIR -ErrorAction SilentlyContinue
    Write-Host "No ffmpeg directory found. The release will not include ffmpeg.exe or ffprobe.exe."
}

$env:PYTHONPATH = (Join-Path $RepoRoot "src")

Write-Host "Package version: $packageVersion"
Write-Host "Release version: $releaseVersion"
Write-Host "Release kind: $ReleaseKind"

& $PythonExe -m PyInstaller --noconfirm --clean packaging\VCutStudio.spec --distpath dist --workpath build\pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$distDir = Join-Path $RepoRoot ("dist\" + $AppBundleName)
$exePath = Join-Path $distDir $AppExecutableName
if (-not (Test-Path $exePath)) {
    throw "Build finished but the executable was not found: $exePath"
}

$distDocsDir = Join-Path $distDir "docs"
New-Item -ItemType Directory -Force -Path $distDocsDir | Out-Null
Copy-Item (Join-Path $RepoRoot "README.md") (Join-Path $distDir "README.md") -Force
$windowsGuide = Get-ChildItem (Join-Path $RepoRoot "docs") -Filter "Windows*.md" | Select-Object -First 1
$otherGuides = Get-ChildItem (Join-Path $RepoRoot "docs") -Filter "*.md" | Where-Object { $_.Name -ne $windowsGuide.Name }
$acceptanceGuide = $otherGuides | Select-Object -First 1
if (-not $windowsGuide -or -not $acceptanceGuide) {
    throw "Could not find the release documentation files in the docs directory."
}
Copy-Item $windowsGuide.FullName (Join-Path $distDocsDir $windowsGuide.Name) -Force
Copy-Item $acceptanceGuide.FullName (Join-Path $distDocsDir $acceptanceGuide.Name) -Force
if ($env:VCUT_BUNDLE_FFMPEG_DIR) {
    $ffmpegBinary = Join-Path $env:VCUT_BUNDLE_FFMPEG_DIR "ffmpeg.exe"
    $ffprobeBinary = Join-Path $env:VCUT_BUNDLE_FFMPEG_DIR "ffprobe.exe"
    if (Test-Path $ffmpegBinary) {
        Copy-Item $ffmpegBinary (Join-Path $distDir "ffmpeg.exe") -Force
    }
    if (Test-Path $ffprobeBinary) {
        Copy-Item $ffprobeBinary (Join-Path $distDir "ffprobe.exe") -Force
    }
}
if ($resolvedStandaloneFfprobePath) {
    Copy-Item $resolvedStandaloneFfprobePath (Join-Path $distDir "ffprobe.exe") -Force
}

$appSettingsPath = Get-AppSettingsPath
$currentSettings = $null
if ($appSettingsPath) {
    $currentSettingsJson = [System.IO.File]::ReadAllText($appSettingsPath)
    $currentSettings = $currentSettingsJson | ConvertFrom-Json
}

$portableRelativeAsrModelDir = ""
if (-not $SkipBundledAsrModel) {
    $bundledAsrModel = Get-BundledAsrModelPlan
    if ($bundledAsrModel) {
        $portableRelativeAsrModelDir = Join-Path "models\asr" $bundledAsrModel.ModelName
        $targetModelDir = Join-Path $distDir $portableRelativeAsrModelDir
        if (Test-Path $targetModelDir) {
            Remove-Item $targetModelDir -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $targetModelDir | Out-Null
        Copy-Item (Join-Path $bundledAsrModel.SourcePath "*") $targetModelDir -Recurse -Force
        Write-Host "Bundled ASR model: $($bundledAsrModel.ModelName)"
        Write-Host "ASR model source: $($bundledAsrModel.SourcePath)"
        $currentSettings = $bundledAsrModel.Settings
    }
    else {
        Write-Host "No local ASR model snapshot was found. Continuing without bundled ASR model."
    }
}

$bundledTtsPlan = Get-BundledTtsPlan
if ($bundledTtsPlan) {
    $targetTtsRoot = Join-Path $distDir "models\tts"
    if (Test-Path $targetTtsRoot) {
        Remove-Item $targetTtsRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $targetTtsRoot | Out-Null
    Copy-Item (Join-Path $bundledTtsPlan.RootPath "*") $targetTtsRoot -Recurse -Force
    Get-ChildItem $targetTtsRoot -Recurse -File |
        Where-Object {
            $_.Name.EndsWith(".tar.bz2", [System.StringComparison]::OrdinalIgnoreCase) -or
            $_.Extension -in @(".zip", ".7z", ".tar", ".gz", ".bz2", ".xz")
        } |
        Remove-Item -Force
    Write-Host "Bundled offline TTS assets: $($bundledTtsPlan.RootPath)"
}
else {
    Write-Host "No local offline TTS assets were found under models\\tts."
}

if ($currentSettings -or $bundledTtsPlan -or $portableRelativeAsrModelDir) {
    Write-PortableSettings `
        -PortableSettingsPath (Join-Path $distDir "settings.json") `
        -CurrentSettings $currentSettings `
        -RelativeModelDir $portableRelativeAsrModelDir `
        -ForceLocalKokoroTts:([bool]$bundledTtsPlan)
    Write-Host "Portable settings created: $(Join-Path $distDir 'settings.json')"
}

Write-Host "Release directory created: $distDir"

if (-not $SkipZip) {
    $releaseDir = Join-Path $RepoRoot "release"
    New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
    $zipPath = Join-Path $releaseDir ($ReleaseArchivePrefix + $releaseVersion + ".zip")
    $zipSucceeded = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (Test-Path $zipPath) {
            Remove-Item $zipPath -Force
        }
        Start-Sleep -Seconds 3
        try {
            Compress-Archive -Path (Join-Path $distDir "*") -DestinationPath $zipPath -Force
            $zipSucceeded = $true
            break
        }
        catch {
            if ($attempt -eq 3) {
                throw
            }
        }
    }
    if (-not $zipSucceeded) {
        throw "Could not create the release archive."
    }
    Write-Host "Release archive created: $zipPath"
}

if ($ReleaseKind -ne "none") {
    Save-ReleaseVersionPlan -StatePath $releaseStatePath -Plan $releasePlan
    Write-Host "Release version state updated: $releaseVersion"
}
