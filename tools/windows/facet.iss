; Facet Windows installer (per-user install, no admin needed).
; Built by tools/build_windows.py, which passes AppVersion / SourceDir /
; OutputDir / IconFile on the ISCC command line.
#define AppName "Facet"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\..\build\windows\Facet"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\windows"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\windows\facet.ico"
#endif

[Setup]
AppId={{B3A0F4A1-9A50-4A7E-9C5E-1E7E28F0C0D2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Facet
DefaultDirName={localappdata}\Programs\Facet
DefaultGroupName=Facet
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Facet-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\app\facet.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
; The browser agent keeps its Chromium under {app}\ms-playwright: never let the
; uninstaller follow the data directory it created at runtime.
UninstallDisplayName={#AppName} 网关

; Simplified Chinese is a community translation (not bundled with Inno Setup);
; the .isl ships next to this .iss. A single [Languages] entry means the
; wizard runs in Chinese directly, with no language-picker page.
; Silent installs can still override with /LANG=chs.

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"
Name: "autostart"; Description: "开机自动启动（托盘常驻）"; GroupDescription: "附加选项:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[INI]
; Placeholder so the shortcut file exists for [Icons] below; the port chosen
; in the wizard is written by app\set_port.py at ssPostInstall.
Filename: "{app}\打开控制台.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://127.0.0.1:8787/"

[Icons]
Name: "{group}\Facet（托盘启动）"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\facet_tray.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\facet.ico"
Name: "{group}\打开控制台"; Filename: "{app}\打开控制台.url"; IconFilename: "{app}\app\facet.ico"
Name: "{group}\数据目录"; Filename: "{app}\data"
Name: "{group}\卸载 Facet"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Facet"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\facet_tray.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\facet.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FacetGateway"; ValueData: """{app}\runtime\pythonw.exe"" ""{app}\app\facet_tray.py"" --quiet"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\facet_tray.py"""; WorkingDir: "{app}"; Description: "启动 Facet 托盘（网关在后台运行）"; Flags: nowait postinstall skipifsilent
Filename: "{app}\打开控制台.url"; Description: "打开管理控制台"; Flags: shellexec nowait postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{app}\stop.cmd"; Flags: runhidden; RunOnceId: "StopTray"

[UninstallDelete]
; The console shortcut is written as an .url file (not a tracked shortcut), so
; remove it explicitly. {app}\data is kept on purpose: it holds router.json,
; keys and the browser profile, i.e. the user's own configuration.
Type: files; Name: "{app}\打开控制台.url"

[Code]
var
  PortPage: TInputQueryWizardPage;

// An in-place upgrade must not fight a running tray for file locks: stop it
// first (fresh installs have no stop.cmd yet, so this is a no-op there).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\stop.cmd')) then
    Exec(ExpandConstant('{cmd}'), '/C ""' + ExpandConstant('{app}\stop.cmd') + '""',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function ValidPort(S: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  if (Length(S) = 0) or (Length(S) > 5) then
    Exit;
  for I := 1 to Length(S) do
    if (S[I] < '0') or (S[I] > '9') then
      Exit;
  Result := (StrToInt(S) >= 1024) and (StrToInt(S) <= 65535);
end;

procedure InitializeWizard();
begin
  PortPage := CreateInputQueryPage(wpSelectTasks,
    '网关端口', '管理控制台和 API 监听的端口',
    '默认 8787。端口被占用或想运行多个实例时可以修改。升级安装时保留原默认值即可，已配置的自定义端口不会被覆盖。');
  PortPage.Add('端口号:', False);
  if ExpandConstant('{param:PORT}') <> '' then
    PortPage.Values[0] := ExpandConstant('{param:PORT}')
  else
    PortPage.Values[0] := '8787';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = PortPage.ID then
    Result := ExpandConstant('{param:PORT}') <> '';  // /PORT= given: apply it silently
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PortPage.ID then
    if not ValidPort(Trim(PortPage.Values[0])) then begin
      MsgBox('请输入 1024-65535 之间的端口号。', mbError, MB_OK);
      Result := False;
    end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
    // Write the chosen port into data\router.json and refresh the console
    // shortcut; the tray and agent read their port from the same file.
    Exec(ExpandConstant('{app}\runtime\python.exe'),
         '"' + ExpandConstant('{app}\app\set_port.py') + '" --apply ' + Trim(PortPage.Values[0]),
         ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
