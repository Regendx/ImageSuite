#define MyAppName "ImageSuite"
#define MyAppVersion "0.9.0 RC33"
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
OutputBaseFilename=ImageSuite-Setup-v0.9.0-RC33
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
ChangesAssociations=yes
VersionInfoVersion=0.9.0.33
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=ImageSuite installer
VersionInfoProductName={#MyAppName}

[Files]
Source: "dist\ImageSuite\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "explorermenu"; Description: "Add Open in ImageSuite to the File Explorer right-click menu"; GroupDescription: "File Explorer integration:"; Flags: checkedonce

[Icons]
Name: "{group}\ImageSuite"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\ImageSuite"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Dedicated shell verbs. These are not Open With registrations and do not claim default file associations.
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\image\shell\ImageSuite"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Open in ImageSuite"; Tasks: explorermenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\image\shell\ImageSuite"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName},0"; Tasks: explorermenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\image\shell\ImageSuite"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Player"; Tasks: explorermenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\image\shell\ImageSuite\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: explorermenu

Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ImageSuite"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Open in ImageSuite"; Tasks: explorermenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ImageSuite"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName},0"; Tasks: explorermenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ImageSuite"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Player"; Tasks: explorermenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ImageSuite\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: explorermenu

Root: HKCU; Subkey: "Software\Classes\Directory\shell\ImageSuite"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Open folder in ImageSuite"; Tasks: explorermenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\ImageSuite"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName},0"; Tasks: explorermenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\ImageSuite\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%V"""; Tasks: explorermenu

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ImageSuite"; Flags: nowait postinstall skipifsilent
