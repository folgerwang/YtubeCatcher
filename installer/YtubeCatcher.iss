; YtubeCatcher installer (Inno Setup 6). Built by build_installer.ps1, which stages the files
; (app + private Python runtime + libmpv) and passes AppVersion / StageDir / OutDir.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\build\installer\stage"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif
#define AppName "YtubeCatcher"
#define AppUserModelID "folgerwang.YtubeCatcher"

[Setup]
AppId={{B9C423F7-7D4E-4661-9484-53CEFF649EF0}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=folgerwang
AppPublisherURL=https://github.com/folgerwang/YtubeCatcher
AppSupportURL=https://github.com/folgerwang/YtubeCatcher/issues
; per-user install: no admin prompt, and the app folder stays writable (settings, yt-dlp updates, BGM filter)
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile={#StageDir}\ytubecatcher.ico
UninstallDisplayIcon={app}\ytubecatcher.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\ytubeviewer.py"""; \
    WorkingDir: "{app}"; IconFilename: "{app}\ytubecatcher.ico"; AppUserModelID: "{#AppUserModelID}"; \
    Comment: "YouTube player + clip extractor"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\ytubeviewer.py"""; \
    WorkingDir: "{app}"; IconFilename: "{app}\ytubecatcher.ico"; AppUserModelID: "{#AppUserModelID}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\ytubeviewer.py"""; WorkingDir: "{app}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; everything the app created after install: settings, sign-in profile, voice models, pip-installed
; updates / BGM filter, deno.exe, caches
Type: filesandordirs; Name: "{app}"
