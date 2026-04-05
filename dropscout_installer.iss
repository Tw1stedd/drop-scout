; Drop Scout — Inno Setup Installer Script
; Compile with Inno Setup 6.x: https://jrsoftware.org/isinfo.php
;
; To build: Open this file in Inno Setup Compiler and click Build > Compile
; Output: Output/DropScout-Setup-v2.0.exe

#define MyAppName "Drop Scout"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "Drop Scout"
#define MyAppExeName "DropScout.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=DropScout-Setup-v{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Require admin for Program Files install
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Allow user to choose install dir
AllowNoIcons=yes
; Uninstaller
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; Close running instance before install (for updates)
CloseApplications=force
CloseApplicationsFilter=DropScout.exe
RestartApplications=yes
; App info
AppContact=support@dropscout.app
AppSupportURL=https://github.com/dropscout
VersionInfoVersion={#MyAppVersion}.0

; Uncomment if you have an icon file:
; SetupIconFile=dropscout.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startupicon"; Description: "Start Drop Scout with Windows"; GroupDescription: "Startup:"

[Files]
; Copy the entire PyInstaller output folder
Source: "dist\DropScout\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Keep user data in AppData (survives updates)
; Config and history files go to %APPDATA%\DropScout

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: dirifempty; Name: "{app}\__pycache__"
Type: dirifempty; Name: "{app}\build"

[Code]
// Kill any running instances before install
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  Exec('taskkill', '/F /IM DropScout.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
