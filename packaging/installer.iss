#define AppName "Live Recording Analyzer"
#define AppExe "Live Recording Analyzer.exe"
#define OldName "OBS Session Analyzer"
#ifndef AppVersion
  #define AppVersion "1.3.0"
#endif

[Setup]
; Same AppId as 1.1 (OBS Session Analyzer), so this upgrade replaces it instead of installing a second copy.
AppId={{6E3C4B1A-2F7D-4C8E-9A51-0B5D7E21C9A4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=bloodrive65
AppPublisherURL=https://bloodrive65.github.io/live-recording-analyzer/
AppSupportURL=https://bloodrive65.github.io/live-recording-analyzer/faq.html
AppUpdatesURL=https://bloodrive65.github.io/live-recording-analyzer/releases.html
DefaultDirName={localappdata}\Programs\{#AppName}
UsePreviousAppDir=no
DefaultGroupName={#AppName}
UsePreviousGroup=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Live-Recording-Analyzer-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start with Windows (minimized to the tray)"; GroupDescription: "Options:"; Flags: unchecked

[InstallDelete]
; Remove the old 1.1 program folder and shortcuts. Your sessions, reports and settings in Documents are kept
; (the app moves them from Documents\OBS Session Analyzer to Documents\Live Recording Analyzer on first start).
Type: filesandordirs; Name: "{localappdata}\Programs\{#OldName}"
Type: filesandordirs; Name: "{userprograms}\{#OldName}"
Type: files; Name: "{userdesktop}\{#OldName}.lnk"
Type: files; Name: "{userstartup}\{#OldName}.lnk"
Type: filesandordirs; Name: "{commonprograms}\{#OldName}"
Type: files; Name: "{commondesktop}\{#OldName}.lnk"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
; After an in-app update (silent install), open the app again by itself.
Filename: "{app}\{#AppExe}"; Parameters: "--updated"; Flags: nowait; Check: WizardSilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { Close a running copy of the old or new version so its files can be replaced. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im "{#OldName}.exe"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im "{#AppExe}"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
