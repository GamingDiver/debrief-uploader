#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{29a26379-a178-4913-9a1c-d982948b5d92}
AppName=Debrief Uploader
AppVersion={#AppVersion}
DefaultDirName={autopf}\DebriefUploader
DefaultGroupName=Debrief Uploader
OutputDir=installer_output
OutputBaseFilename=DebriefUploader_Setup_{#AppVersion}
SetupIconFile=debrief_uploader\resources\app.ico
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no

[Files]
Source: "dist\DebriefUploader.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Debrief Uploader"; Filename: "{app}\DebriefUploader.exe"; Parameters: "run --tray"
Name: "{autodesktop}\Debrief Uploader"; Filename: "{app}\DebriefUploader.exe"; Parameters: "run --tray"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"

[Run]
Filename: "{app}\DebriefUploader.exe"; Parameters: "run --tray"; Description: "Launch Debrief Uploader"; Flags: nowait postinstall skipifsilent