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
; Ticked by default: an uploader that does not start after a reboot uploads
; nothing (2026-10-12). Same Run value Settings writes; it can be switched off
; there or in Task Manager -> Startup apps.
Name: "autostart"; Description: "Start automatically when I log in"

[Registry]
; HKCU, like Settings: no admin rights needed. The data must stay identical to
; autostart.command() for the frozen exe, or the app reads it as stale.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "GamingDiver Debrief Uploader"; ValueData: """{app}\DebriefUploader.exe"" run --tray --quiet"; Tasks: autostart

[Run]
; runasoriginaluser: an all-users install runs Setup elevated, and the app
; must not inherit that.
Filename: "{app}\DebriefUploader.exe"; Parameters: "run --tray"; Description: "Launch Debrief Uploader"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
// "Start automatically when I log in" (Settings) writes this HKCU Run value.
// Remove it on uninstall, but only if it points at THIS exe: a zip install of
// the same app uses the same value name and must keep starting.
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunName = 'GamingDiver Debrief Uploader';

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cmd: String;
begin
  if CurUninstallStep <> usUninstall then Exit;
  if RegQueryStringValue(HKCU, RunKey, RunName, Cmd) and
     (Pos(Lowercase(ExpandConstant('{app}\DebriefUploader.exe')), Lowercase(Cmd)) > 0) then
    RegDeleteValue(HKCU, RunKey, RunName);
end;
