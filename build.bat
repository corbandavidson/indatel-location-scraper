@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Location Scraper - Desktop Build
echo ============================================
echo.

set "SRC_DIR=%~dp0"
set "DIST=dist\LocationScraper"
set "PY_EMBED=%DIST%\python"
set "PY_VERSION=3.11.9"
set "PY_ZIP=python-3.11.9-embed-amd64.zip"
set "PY_URL=https://www.python.org/ftp/python/3.11.9/%PY_ZIP%"
set "GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"

:: Check Python (for building the launcher)
py --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ and try again.
    pause
    exit /b 1
)

:: Clean previous build
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

:: ─── Step 1: Build the launcher exe ──────────────────────────────────
echo [1/6] Building launcher exe...
py -m pip install --quiet pyinstaller
py -m PyInstaller --clean --noconfirm LocationScraper.spec
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

:: ─── Step 2: Download embedded Python ────────────────────────────────
echo [2/6] Downloading embedded Python %PY_VERSION%...
if not exist "%PY_ZIP%" (
    powershell -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_ZIP%'"
)
if not exist "%PY_ZIP%" (
    echo ERROR: Failed to download Python embeddable package.
    pause
    exit /b 1
)
echo   Extracting...
mkdir "%PY_EMBED%" 2>nul
powershell -Command "Expand-Archive -Path '%PY_ZIP%' -DestinationPath '%PY_EMBED%' -Force"

:: Enable site-packages (write without BOM)
powershell -Command "[IO.File]::WriteAllText('%PY_EMBED%\python311._pth', \"python311.zip`n.`nimport site\", [Text.UTF8Encoding]::new($false))"

:: ─── Step 3: Install pip + packages into embedded Python ─────────────
echo [3/6] Installing packages into embedded Python...
if not exist get-pip.py (
    powershell -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile 'get-pip.py'"
)
"%PY_EMBED%\python.exe" get-pip.py --quiet

:: Install all requirements (includes streamlit, pandas, etc.)
"%PY_EMBED%\python.exe" -m pip install --quiet -r requirements.txt

:: Force reinstall streamlit to ensure all transitive deps are present
"%PY_EMBED%\python.exe" -m pip install --quiet --force-reinstall streamlit

:: Reinstall app requirements that streamlit --force-reinstall may have broken
"%PY_EMBED%\python.exe" -m pip install --quiet python-dotenv fake-useragent usaddress geopy duckduckgo-search tenacity

:: ─── Step 4: Install Playwright ──────────────────────────────────────
echo [4/6] Installing Playwright browsers...
"%PY_EMBED%\python.exe" -m pip install --quiet playwright
"%PY_EMBED%\python.exe" -m playwright install chromium

:: ─── Step 5: Copy application code ──────────────────────────────────
echo [5/6] Copying application files...
set "APP_DIR=%DIST%\app"
mkdir "%APP_DIR%\config" 2>nul
mkdir "%APP_DIR%\scraper" 2>nul
mkdir "%APP_DIR%\.streamlit" 2>nul

copy app.py "%APP_DIR%\" >nul
copy main.py "%APP_DIR%\" >nul
copy logo.svg "%APP_DIR%\" >nul
if exist .env copy .env "%APP_DIR%\" >nul
xcopy config "%APP_DIR%\config\" /E /I /Q /Y >nul
xcopy scraper "%APP_DIR%\scraper\" /E /I /Q /Y >nul
xcopy .streamlit "%APP_DIR%\.streamlit\" /E /I /Q /Y >nul

:: ─── Step 6: Copy Playwright browsers ────────────────────────────────
echo [6/6] Bundling Playwright browsers...
set "PW_BROWSERS=%USERPROFILE%\AppData\Local\ms-playwright"

if exist "%PW_BROWSERS%" (
    for /D %%d in ("%PW_BROWSERS%\chromium_headless_shell-*") do (
        echo   Copying headless shell: %%~nxd
        xcopy "%%d" "%DIST%\playwright-browsers\%%~nxd\" /E /I /Q /Y >nul 2>&1
    )
    for /D %%d in ("%PW_BROWSERS%\ffmpeg-*") do (
        xcopy "%%d" "%DIST%\playwright-browsers\%%~nxd\" /E /I /Q /Y >nul 2>&1
    )
    if exist "%PW_BROWSERS%\.links" (
        xcopy "%PW_BROWSERS%\.links" "%DIST%\playwright-browsers\.links\" /E /I /Q /Y >nul 2>&1
    )
) else (
    echo   WARNING: Playwright browsers not found.
)

:: Create output directory
mkdir "%DIST%\output" 2>nul

:: Verify
echo.
echo   Verifying installation...
"%PY_EMBED%\python.exe" -c "import streamlit, pandas, dotenv, bs4, requests; print('  All imports OK')"

echo.
echo ============================================
echo   BUILD COMPLETE
echo ============================================
echo.
echo   Output:  dist\LocationScraper\
echo   Run:     dist\LocationScraper\LocationScraper.exe
echo.
echo   To share: zip the dist\LocationScraper folder.
echo.
pause
