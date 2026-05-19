; Inno Setup script for LocationScraperAI.
;
; Compiled via build.ps1 — version is overridden with /DMyAppVersion=X.Y.Z
; so the installer metadata stays in sync with ai_version/version.py.
;
; Per-user install (no admin needed). The stable AppId means re-running a
; newer installer cleanly replaces the previous version's files without
; asking the user.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName       "Location Scraper AI"
#define MyAppPublisher  "INDATEL Labs"
#define MyAppURL        "https://github.com/corbandavidson/indatel-location-scraper"
#define MyAppExeName    "LocationScraperAI.exe"
#define MyAppDistFolder "..\dist\LocationScraperAI"

[Setup]
; Stable identifier — never change this between releases or the installer
; will treat updates as a fresh side-by-side install.
AppId={{8B3F4D5E-9A2C-4E8F-A1B7-C5D6E7F8A9B0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases

; Per-user install — drops into LocalAppData so we never need admin
DefaultDirName={localappdata}\Programs\LocationScraperAI
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

OutputDir=Output
OutputBaseFilename=LocationScraperAI-Installer-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Show our license / readme nothing fancy; keep wizard short
DisableWelcomePage=no
DisableReadyPage=no

UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Bundle the entire built dist folder. recursesubdirs + createallsubdirs
; preserves the Playwright browsers layout. ignoreversion is required for
; bundled DLLs without version info.
Source: "{#MyAppDistFolder}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Logs/output the running app writes into its install dir. User settings
; live in %USERPROFILE%\.location_scraper and are intentionally preserved.
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\output"
Type: files; Name: "{app}\launcher.log"
