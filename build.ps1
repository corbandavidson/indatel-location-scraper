<#
.SYNOPSIS
  Build (and optionally publish) a Location Scraper AI release.

.DESCRIPTION
  Bumps ai_version/version.py, syncs source code into the existing
  PyInstaller dist tree, compiles the Inno Setup installer, and -- with
  -Publish -- commits, tags, pushes, and creates a GitHub release with
  the installer attached.

  Assumes:
    - Inno Setup 6 installed at the default path
    - PyInstaller dist already exists at .\dist\LocationScraperAI
    - gh CLI authenticated (for -Publish)

.PARAMETER Version
  Semantic version, e.g. 1.0.1. Required.

.PARAMETER Publish
  After building the installer, commit/tag/push and create the GitHub
  release. Without this flag the script just builds locally so you can
  test the installer before publishing.

.PARAMETER SkipBump
  Don't rewrite version.py. Useful when rebuilding the same version
  after fixing something in the installer config.

.PARAMETER FullRebuild
  Re-install all pip packages from requirements.txt into the embedded
  Python before syncing source and building the installer. Required
  when a new dependency is added (e.g. curl_cffi). Also re-installs
  Playwright browsers to pick up any new ones.

  This does NOT rebuild the PyInstaller launcher exe — that's only
  needed if desktop_ai.py or the .spec file changed (rare; run
  pyinstaller manually for that).

.EXAMPLE
  .\build.ps1 -Version 1.0.1
  # builds installer at installer\Output\LocationScraperAI-Setup-1.0.1.exe

.EXAMPLE
  .\build.ps1 -Version 1.0.1 -Publish
  # builds, commits, tags, pushes, creates v1.0.1 GitHub release

.EXAMPLE
  .\build.ps1 -Version 1.1.0 -FullRebuild -Publish
  # installs new deps, builds installer, publishes
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [switch]$Publish,

    [switch]$SkipBump,

    [switch]$FullRebuild
)

$ErrorActionPreference = "Stop"
$Root             = $PSScriptRoot
$VersionFile      = Join-Path $Root "ai_version\version.py"
$IscPath          = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
$IssFile          = Join-Path $Root "installer\LocationScraperAI.iss"
$DistFolder       = Join-Path $Root "dist\LocationScraperAI"
$DistAppFolder    = Join-Path $DistFolder "app"
$InstallerOutput  = Join-Path $Root "installer\Output\LocationScraperAI-Installer-$Version.exe"

function Write-Step($msg) {
    Write-Host ""
    Write-Host ">>> $msg" -ForegroundColor Cyan
}

# ── Pre-flight ──────────────────────────────────────────────────────
if (-not (Test-Path $IscPath)) {
    throw "Inno Setup not found at $IscPath. Install Inno Setup 6."
}
if (-not (Test-Path $DistFolder)) {
    throw "PyInstaller dist missing at $DistFolder. Run the PyInstaller build first (build.bat)."
}
if (-not (Test-Path $VersionFile)) {
    throw "Version file missing: $VersionFile"
}

# ── 0. Full rebuild — reinstall packages into embedded Python ────────
if ($FullRebuild) {
    $EmbeddedPython = Join-Path $DistFolder "python\python.exe"
    if (-not (Test-Path $EmbeddedPython)) {
        throw "Embedded Python not found at $EmbeddedPython. Run build.bat first to create the dist tree."
    }
    $ReqFile = Join-Path $Root "requirements.txt"

    Write-Step "Installing pip packages into embedded Python"
    & $EmbeddedPython -m pip install --quiet --upgrade pip
    & $EmbeddedPython -m pip install --quiet -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed (exit $LASTEXITCODE)"
    }
    # Verify the new dependency is importable
    & $EmbeddedPython -c "from curl_cffi import requests; print('  curl_cffi OK')" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "curl_cffi did not install correctly — TLS impersonation will be disabled."
    }

    Write-Step "Reinstalling Playwright browsers"
    & $EmbeddedPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Playwright chromium install failed — existing browser will be used."
    }
    # Firefox is optional — install it for the anti-bot fallback tier.
    # If it fails (e.g. disk space) the app still works, just without
    # the Firefox fallback.
    & $EmbeddedPython -m playwright install firefox 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Firefox browser installed (anti-bot fallback)" -ForegroundColor Green
    } else {
        Write-Host "  Firefox install skipped (optional)" -ForegroundColor Yellow
    }

    # Copy updated Playwright browsers into the dist bundle
    $PwBrowsers = Join-Path $env:LOCALAPPDATA "ms-playwright"
    $PwDest = Join-Path $DistFolder "playwright-browsers"
    if (Test-Path $PwBrowsers) {
        Write-Step "Syncing Playwright browsers into dist"
        # Chromium (always)
        Get-ChildItem -Path $PwBrowsers -Directory -Filter "chromium*" | ForEach-Object {
            robocopy $_.FullName (Join-Path $PwDest $_.Name) /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        }
        # Firefox (if installed)
        Get-ChildItem -Path $PwBrowsers -Directory -Filter "firefox*" | ForEach-Object {
            robocopy $_.FullName (Join-Path $PwDest $_.Name) /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        }
        # ffmpeg (shared dependency)
        Get-ChildItem -Path $PwBrowsers -Directory -Filter "ffmpeg*" | ForEach-Object {
            robocopy $_.FullName (Join-Path $PwDest $_.Name) /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        }
    }
}

# ── 1. Bump version.py ──────────────────────────────────────────────
if (-not $SkipBump) {
    Write-Step "Bumping version.py -> $Version"
    $content = Get-Content $VersionFile -Raw
    $newContent = $content -replace '__version__\s*=\s*"[^"]*"', "__version__ = `"$Version`""
    if ($newContent -eq $content) {
        Write-Warning "version.py was not updated (pattern didn't match). Check format."
    }
    Set-Content -Path $VersionFile -Value $newContent -NoNewline -Encoding UTF8
}

# ── 2. Sync source -> dist\LocationScraperAI\app ────────────────────
# Source-only updates ride the existing PyInstaller bundle: we just
# replace the .py files inside app\. Native dependency changes still
# need a full rebuild (build.bat), which this script doesn't do.
Write-Step "Syncing source into dist app folder"
$syncDirs = @("ai_version", "scraper", "config")
foreach ($d in $syncDirs) {
    $srcD = Join-Path $Root $d
    $dstD = Join-Path $DistAppFolder $d
    if (-not (Test-Path $srcD)) {
        Write-Warning "Source dir missing: $srcD (skipping)"
        continue
    }
    if (-not (Test-Path $dstD)) {
        New-Item -ItemType Directory -Path $dstD -Force | Out-Null
    }
    # robocopy is the cleanest cross-version mirror; /MIR mirrors the tree,
    # /XD __pycache__ excludes bytecode dirs, /NFL/NDL/NJH/NJS keep output quiet.
    # robocopy uses exit codes 0-7 for success; treat anything >=8 as failure.
    robocopy $srcD $dstD /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NC /NS | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed (exit $LASTEXITCODE) syncing $d"
    }
}

# ── 3. Clear pycache everywhere ─────────────────────────────────────
Write-Step "Clearing __pycache__ directories"
Get-ChildItem -Path $Root -Recurse -Filter "__pycache__" -Directory -ErrorAction SilentlyContinue `
    | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ── 4. Compile installer ────────────────────────────────────────────
Write-Step "Compiling Inno Setup installer"
& $IscPath "/DMyAppVersion=$Version" $IssFile
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compilation failed (exit code $LASTEXITCODE)"
}
if (-not (Test-Path $InstallerOutput)) {
    throw "Installer not produced at expected path: $InstallerOutput"
}

$sizeMB = [math]::Round((Get-Item $InstallerOutput).Length / 1MB, 1)
Write-Host ""
Write-Host "Built: $InstallerOutput  ($sizeMB MB)" -ForegroundColor Green

# ── 5. Publish (optional) ───────────────────────────────────────────
if ($Publish) {
    Write-Step "Publishing v$Version to GitHub"

    Set-Location $Root

    # git/gh write informational text to stderr (e.g. "Everything up-to-date").
    # Under ErrorActionPreference=Stop, PowerShell treats those as terminating
    # errors. We merge stderr into stdout for these commands and gate on
    # $LASTEXITCODE instead.
    function Invoke-Native {
        param([string]$Label, [scriptblock]$Block)
        $out = & $Block 2>&1
        $code = $LASTEXITCODE
        if ($out) { $out | ForEach-Object { Write-Host $_ } }
        if ($code -ne 0) {
            throw "$Label failed (exit $code)"
        }
    }

    # Stage the version bump (if any) and commit
    Invoke-Native "git add" { git add ai_version/version.py }
    $dirty = (& git status --porcelain) 2>&1
    if ($dirty) {
        Invoke-Native "git commit" { git commit -m "Release v$Version" }
    } else {
        Write-Host "No version.py change to commit"
    }

    # Tag locally — if the tag already exists (re-publish), don't fail
    $existingTag = (& git tag -l "v$Version") 2>&1
    if (-not $existingTag) {
        Invoke-Native "git tag" { git tag "v$Version" }
    }

    Invoke-Native "git push main"   { git push origin main }
    Invoke-Native "git push tag"    { git push origin "v$Version" }

    # Skip release creation if a release for this tag already exists
    $existingRelease = & gh release view "v$Version" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Release v$Version already exists — uploading installer as asset"
        Invoke-Native "gh release upload" {
            gh release upload "v$Version" $InstallerOutput --clobber
        }
    } else {
        $notes = @"
**Location Scraper AI v$Version**

Download the installer below and run it. Per-user install, no admin required.

The app checks GitHub on launch — running an older version, you'll see an update notice in the sidebar.
"@
        Invoke-Native "gh release create" {
            gh release create "v$Version" --title "v$Version" --notes $notes $InstallerOutput
        }
    }

    Write-Host ""
    Write-Host "Published v$Version" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Local build complete. Test the installer, then run with -Publish to release." -ForegroundColor Yellow
}
