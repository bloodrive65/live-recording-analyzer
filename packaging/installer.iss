#define AppName "OBS Session Analyzer"
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

[Setup]
AppId={{6E3C4B1A-2F7D-4C8E-9A51-0B5D7E21C9A4}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=bloodrive65
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=OBS-Session-Analyzer-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\OBS Session Analyzer.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start with Windows (minimized to the tray)"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\dist\OBS Session Analyzer\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\OBS Session Analyzer.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\OBS Session Analyzer.exe"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\OBS Session Analyzer.exe"; Parameters: "--minimized"; Tasks: startup

[Run]
Filename: "{app}\OBS Session Analyzer.exe"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
