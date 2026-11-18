; Inno Setup Script for Vaani Desktop Tracker
; Generates Windows installer (VaaniTrackerSetup.exe) with branded icons, desktop shortcut, and startup integration.

#define MyAppName "Vaani Desktop Tracker"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Vaani Telecalling CRM"
#define MyAppURL "https://vaaniapp.co.in"
#define MyAppExeName "VaaniTracker.exe"

[Setup]
; Basic setup parameters (Strictly Per-User Installation)
AppId={{5E65D7B2-C1F8-4A11-8075-8D6F39F80A90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\VaaniTracker
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=VaaniTrackerSetup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes
Uninstallable=yes

; Running application detection (coordinates with SingleInstanceLock)
AppMutex=Local\VaaniDesktopTrackerRunning
CloseApplications=no

; Branded Icons
SetupIconFile=assets\vaani.ico
UninstallDisplayIcon={app}\assets\vaani.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupreg"; Description: "Launch Vaani Tracker automatically when Windows starts"; GroupDescription: "Windows Integration:"; Flags: unchecked

[InstallDelete]
; Clean up legacy startup shortcut from previous installer versions during install/upgrade to prevent duplicate launches
Type: files; Name: "{userstartup}\{#MyAppName}.lnk"

[Files]
; Packaging PyInstaller onedir distribution layout recursively
Source: "dist\VaaniTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Static visual assets for uninstaller and runtime fallback
Source: "assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
; Documentation and configuration example (runtime config in %LOCALAPPDATA%\VaaniTracker is never touched)
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\vaani.ico"
Name: "{group}\Privacy Policy & Employee Rights"; Filename: "{app}\PRIVACY.md"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"; IconFilename: "{app}\assets\vaani.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\vaani.ico"; Tasks: desktopicon

[Registry]
; Windows user-level startup entry in HKCU Run (aligns with Dashboard Settings)
; Single auditable entry: sets startup on fresh install (if task checked) or preserves/refreshes existing setting on upgrade
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VaaniTracker"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Check: ShouldSetStartup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  LegacyStartupShortcutFound: Boolean;

// Captures legacy shortcut existence early in wizard initialization before [InstallDelete] can remove it
procedure InitializeWizard;
begin
  LegacyStartupShortcutFound := FileExists(ExpandConstant('{userstartup}\{#MyAppName}.lnk'));
end;

// Fallback check immediately before installation steps commence
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    if not LegacyStartupShortcutFound then
    begin
      LegacyStartupShortcutFound := FileExists(ExpandConstant('{userstartup}\{#MyAppName}.lnk'));
    end;
  end;
end;

// Evaluates whether startup should be enabled: user selected task, existing registry setting, OR migrated legacy shortcut
function ShouldSetStartup: Boolean;
begin
  Result := WizardIsTaskSelected('startupreg') or
            RegValueExists(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'VaaniTracker') or
            LegacyStartupShortcutFound;
end;

// Guarantees clean removal of HKCU Run entry on uninstall even if enabled via Dashboard post-installation
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RegValueExists(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'VaaniTracker') then
    begin
      RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'VaaniTracker');
    end;
  end;
end;
