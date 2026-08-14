; Inno Setup script for LoLcal History.
;
; Built by .github/workflows/release.yml, which passes the version in:
;   iscc /DAppVersion=1.2.3 installer\lolcal-history.iss
;
; Two choices here matter more than the rest:
;
;   PrivilegesRequired=lowest installs under %LOCALAPPDATA% with no UAC prompt.
;   That is what lets the in-app update button run this silently — an installer
;   needing administrator rights would pop a consent dialog the app cannot
;   answer, and the update would appear to hang.
;
;   CloseApplications=force, and deliberately no AppMutex. The app hides to the
;   tray rather than quitting when its window is closed, so it refuses the
;   WM_CLOSE that Restart Manager sends politely; without `force`, Setup gives
;   up ("unable to automatically close all applications"), and under
;   /SUPPRESSMSGBOXES that answer becomes a silent cancel and rollback.
;   AppMutex would be worse still: the update button launches Setup from inside
;   the very app that is about to exit, so the mutex is always still held at
;   the moment Setup looks, and Setup would abort before starting.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

; The displayed version may carry a label ("0.2.0-rc1"); the one Windows stores
; in the file's version resource may not — Inno rejects anything that is not
; purely numeric. The workflow passes the stripped form separately.
#ifndef NumericVersion
  #define NumericVersion AppVersion
#endif

#define AppName "LoLcal History"
#define AppExe "LoLcal History.exe"
#define AppPublisher "KioCoan"
#define AppUrl "https://github.com/KioCoan/LoLcalHistory"

[Setup]
; Never change this GUID. It is how Windows recognises an upgrade of an
; existing install rather than a second, parallel one.
AppId={{8F3A6C21-5D74-4B2E-9A18-6C0E7B4D91F2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#NumericVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases

DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

OutputDir=..\dist\installer
OutputBaseFilename=LoLcal-History-{#AppVersion}-Setup
SetupIconFile=..\lolhist\assets\icon.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

; Close a running copy before replacing its files. `force` terminates what will
; not close on its own — see the note above. Safe here: the database is SQLite
; in WAL mode, which treats an abrupt exit the same as a power cut, and the app
; is relaunched by [Run] either way.
CloseApplications=force
RestartApplications=no
SetupMutex={#AppName}-setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start {#AppName} when I sign in"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; \
    Flags: uninsdeletevalue; Tasks: startup
; A previous install may have set this. If the box is now unticked, clear it
; rather than leaving the app starting from a path the user no longer wants.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: none; ValueName: "{#AppName}"; \
    Flags: deletevalue; Tasks: not startup

[Run]
; `skipifsilent` on purpose. A silent install is an in-app update, and starting
; the app from here launches it the instant the new 27 MB executable is written
; — racing the on-access virus scanner still holding that file. A one-file build
; unpacks itself at startup, so it loses that race and dies with
; "Failed to load Python DLL ...\_MEIxxxxx\python3xx.dll". The updater relaunches
; it a few seconds later instead, where there is somewhere to pause.
; An interactive install still offers to open it, as anyone would expect.
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leaves %LOCALAPPDATA%\LoLcal History alone — that is the match history, and
; an uninstall must never be the thing that deletes it.
Type: files; Name: "{app}\*.log"
