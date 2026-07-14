#define MyAppName "ImageSuite"
#define MyAppVersion "0.9.0 RC22"
#define MyAppPublisher "Regendx"
#define MyAppExeName "ImageSuite.exe"

[Setup]
AppId={{E41D2034-D23D-4E0B-AB37-7AD1B45461A1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ImageSuite
DefaultGroupName=ImageSuite
PrivilegesRequired=lowest
OutputDir=release
OutputBaseFilename=ImageSuite-Setup-v0.9.0-RC22
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\ImageSuite\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ImageSuite"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\ImageSuite"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ImageSuite"; Flags: nowait postinstall skipifsilent
