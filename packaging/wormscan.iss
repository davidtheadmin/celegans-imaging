; ---------------------------------------------------------------------------
; WormScan installer
;
; Built by build_installer.ps1, which passes the payload location and version
; in on the command line. Do not run ISCC on this file directly.
;
; Design notes worth knowing before editing:
;
;  * Per-user install, no admin. PrivilegesRequired=lowest means no UAC prompt
;    at any point, which matters on a managed university laptop where the user
;    may simply not have admin rights. It also keeps the install directory
;    writable, which is what lets the two virtual environments live inside it.
;
;  * Nothing is compiled. The app ships as .py source. A fix can be delivered
;    by replacing a file; a retrained model needs no rebuild at all.
;
;  * The venvs are built at install time from bundled wheels (postinstall.ps1),
;    not shipped pre-made. A venv records absolute paths, so a pre-made one
;    would break the moment the install directory differed from the build
;    machine's.
; ---------------------------------------------------------------------------

#ifndef PayloadDir
  #error Run build_installer.ps1 instead of invoking ISCC directly.
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef FullVersion
  #define FullVersion AppVersion
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif

#define AppName      "WormScan"
#define AppPublisher "C. elegans imaging"
#define AppExeName   "pythonw.exe"

[Setup]
AppId={{8C4E1F2A-7B3D-4E9A-9C15-2D6F8A0B4E77}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#FullVersion}
AppPublisher={#AppPublisher}
; AGPL-3.0 section 13: recipients must be able to get the corresponding source.
; These show as links in Windows' Installed Apps entry.
AppPublisherURL=https://github.com/davidtheadmin/celegans-imaging
AppSupportURL=https://github.com/davidtheadmin/celegans-imaging
AppUpdatesURL=https://github.com/davidtheadmin/celegans-imaging/releases
VersionInfoVersion={#AppVersion}

; Per-user. No admin, no UAC, no elevation dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Not {localappdata}\Programs\... : dropping 'Programs' buys 9 characters of
; MAX_PATH headroom, and torch's licence tree needs 167 characters on its own.
; See the check at the top of postinstall.ps1.
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no

LicenseFile={#PayloadDir}\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=WormScanSetup-{#AppVersion}
SetupIconFile={#PayloadDir}\app\launcher\assets\wormscan.ico
UninstallDisplayIcon={app}\app\launcher\assets\wormscan.ico
UninstallDisplayName={#AppName} {#FullVersion}

; LZMA2/max on a ~500 MB payload: slower to build, materially smaller to send.
Compression=lzma2/max
SolidCompression=yes
#if Ver >= EncodeVer(6,3,0,0)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#else
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
#endif

WizardStyle=modern
ShowLanguageDialog=no
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole staged payload. Order does not matter; postinstall runs after.
Source: "{#PayloadDir}\python\*";  DestDir: "{app}\python";  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\ffmpeg\*";  DestDir: "{app}\ffmpeg";  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\app\*";     DestDir: "{app}\app";     Flags: ignoreversion recursesubdirs createallsubdirs
; Wheels are deleted by postinstall once the venvs exist - they are ~450 MB of
; install-time scaffolding, not something to leave on the user's disk.
Source: "{#PayloadDir}\wheels\*";  DestDir: "{app}\wheels";  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\install-space.json"; DestDir: "{app}"; Flags: ignoreversion
; AGPL-3.0: the licence and the third-party notices must accompany the work.
Source: "{#PayloadDir}\LICENSE";                DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "postinstall.ps1";         DestDir: "{app}\tools";   Flags: ignoreversion
Source: "setup_engine.ps1";        DestDir: "{app}\tools";   Flags: ignoreversion

[Icons]
; Targets the venv's pythonw.exe, exactly as the dev shortcut always has, so
; the installed and development launch paths stay identical.
Name: "{group}\{#AppName}"; \
    Filename: "{app}\venv\Scripts\pythonw.exe"; \
    Parameters: """{app}\app\launcher\main.py"""; \
    WorkingDir: "{app}\app"; \
    IconFilename: "{app}\app\launcher\assets\wormscan.ico"

Name: "{group}\{#AppName} - Set up video analysis"; \
    Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\setup_engine.ps1"""; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\app\launcher\assets\wormscan.ico"; \
    Comment: "Installs Rancher Desktop and downloads Tierpsy. Only needed for Motility and Crawling."

Name: "{group}\{#AppName} data folder"; \
    Filename: "{userappdata}\WormScan"; \
    Comment: "Logs, settings, and any model or threshold overrides"

Name: "{userdesktop}\{#AppName}"; \
    Filename: "{app}\venv\Scripts\pythonw.exe"; \
    Parameters: """{app}\app\launcher\main.py"""; \
    WorkingDir: "{app}\app"; \
    IconFilename: "{app}\app\launcher\assets\wormscan.ico"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\venv\Scripts\pythonw.exe"; \
    Parameters: """{app}\app\launcher\main.py"""; \
    WorkingDir: "{app}\app"; \
    Description: "Start {#AppName} now"; \
    Check: LauncherIsReady; \
    Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Created after install, so Inno does not know about them.
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\venv-vision"
Type: filesandordirs; Name: "{app}\wheels"
Type: filesandordirs; Name: "{app}\app\launcher\__pycache__"
Type: dirifempty;     Name: "{app}"
; NOTE: %APPDATA%\WormScan is deliberately NOT removed. It holds the user's
; settings, their log, and any model or threshold overrides they were given.
; Uninstalling the app should not throw those away.

[Code]

{ Used to spot a network drive before installing onto one. }
function GetDriveType(lpRootPathName: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

const
  DRIVE_REMOTE = 3;

var
  SetupPage: TOutputProgressWizardPage;
  EnvBuildOk: Boolean;

function LauncherIsReady(): Boolean;
begin
  { Guards the "Start WormScan now" checkbox. Without this, a failed
    environment build still offers to launch an interpreter that is not there. }
  Result := EnvBuildOk and
            FileExists(ExpandConstant('{app}\venv\Scripts\pythonw.exe'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  SitePackages: String;
  Headroom: Integer;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    { Check the chosen folder BEFORE copying 600 MB into it. torch ships a
      licence file 167 characters deep and Windows caps a path at 260, so a
      folder that is merely a little too deep produces an opaque WinError 206
      three minutes into the install. Catching it on the directory page turns
      a baffling failure into a one-line instruction. }
    { A network drive or UNC path is the wrong home for this. Three reasons,
      in order of how quickly they bite:

        1. Installing fails. Antivirus and SMB oplocks hold newly written files
           open, so Inno's rename step returns "MoveFile failed; code 5, access
           denied" - which is what happened on a real attempt with the 122 MB
           torch wheel.
        2. If it did install, it would be slow forever. A venv is tens of
           thousands of small files and torch opens a great many of them at
           import; that is the worst possible workload for SMB.
        3. A venv hard-codes its own absolute path. Remap the drive letter or
           work offline and the app stops starting.

      The right split is the app on a local disk and the MIRROR FOLDER on the
      share - that is where the data volume actually is, and it is plain file
      copying rather than code execution. }
    if (Copy(WizardDirValue, 1, 2) = '\\') or
       (GetDriveType(Copy(WizardDirValue, 1, 3)) = DRIVE_REMOTE) then
    begin
      if MsgBox('That looks like a network drive or shared folder:'#13#10#13#10 +
                WizardDirValue + #13#10#13#10 +
                'Installing here usually FAILS partway through, because antivirus ' +
                'and file locking on shared drives block the installer from ' +
                'renaming files. If it does succeed, WormScan will be very slow ' +
                'to start, and it will stop working entirely if the drive letter ' +
                'changes or you are offline.'#13#10#13#10 +
                'Install on a local disk instead (C: or another built-in drive).'#13#10#13#10 +
                'If you chose this because of disk space: the program needs about ' +
                '2.5 GB locally, but your IMAGES do not have to live here. After ' +
                'installing, set the Mirror folder in Settings to this shared ' +
                'drive - that is where the space actually goes.'#13#10#13#10 +
                'Continue anyway?',
                mbError, MB_YESNO or MB_DEFBUTTON2) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;

    SitePackages := AddBackslash(WizardDirValue) + 'venv-vision\Lib\site-packages';
    Headroom := 260 - 1 - 167 - Length(SitePackages);
    if Headroom < 0 then
    begin
      MsgBox('That folder is too deep for Windows.'#13#10#13#10 +
             WizardDirValue + #13#10#13#10 +
             'Windows limits a file path to 260 characters, and one file inside ' +
             'PyTorch uses 167 of them by itself. This folder needs to be ' +
             IntToStr(-Headroom) + ' characters shorter.'#13#10#13#10 +
             'Please choose something shorter. C:\WormScan always works.',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure InitializeWizard();
begin
  SetupPage := CreateOutputProgressPage(
    'Setting up WormScan',
    'Building the Python environments. This takes a few minutes and needs no input.');
end;

function BuildEnvironments(): Boolean;
var
  ResultCode: Integer;
  PsCmd: String;
begin
  Result := False;

  SetupPage.SetText(
    'Installing dependencies...',
    'A console window will show the progress. Please leave it alone until it closes.');
  SetupPage.SetProgress(0, 100);
  SetupPage.Show();
  try
    PsCmd := '-NoProfile -ExecutionPolicy Bypass -File "' +
             ExpandConstant('{app}\tools\postinstall.ps1') + '" -InstallDir "' +
             ExpandConstant('{app}') + '"';

    { Deliberately NOT runhidden. This step takes several minutes (torch alone
      is ~250 MB to unpack) and a visible pip is the difference between "it is
      working" and "it has frozen". It is also the only diagnostic anyone gets
      if a wheel fails to install. }
    if not Exec('powershell.exe', PsCmd, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('Could not start PowerShell to finish the installation.'#13#10#13#10 +
             'WormScan is copied to disk but its Python environment was not built,' +
             ' so it will not start yet.'#13#10#13#10 +
             'Send David a screenshot of this message.',
             mbCriticalError, MB_OK);
      Exit;
    end;

    if ResultCode <> 0 then
    begin
      MsgBox('Setting up the Python environment failed (code ' + IntToStr(ResultCode) + ').'#13#10#13#10 +
             'A log was written to:'#13#10 +
             ExpandConstant('{app}\install-log.txt') + #13#10#13#10 +
             'Send that file to David - it says exactly which step failed.',
             mbCriticalError, MB_OK);
      Exit;
    end;

    Result := True;
  finally
    SetupPage.Hide();
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    EnvBuildOk := BuildEnvironments();
end;
