; TriMCP Inno Setup — Phase 5 seven-screen flow with branching (Local / Office Shared / Cloud).
;
; Shim: compiler cwd is build/windows/. trimcp-launch.exe is produced by
;       go/cmd/trimcp-launch → ../../../build/windows/trimcp-launch.exe (.github/workflows/release.yml).
;
; Silent install: TriMCP-Setup.exe /VERYSILENT /MODE=cloud /TENANT=contoso.onmicrosoft.com /BACKEND=cuda
;               /MODE=multiuser /SERVERADDR=https://trimcp.corp.example
;               /BRIDGES=sharepoint,dropbox /BACKEND=auto
;
; Writes UTF-8 %APPDATA%\TriMCP\mode.txt (local | multiuser | cloud) and .env (TRIMCP_* keys).

#define MyAppName "TriMCP"
#define MyAppVersion "1.0.0"

[Setup]
AppId={{8E7F6D5C-4B3A-2918-CDEF-0123456789AB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=TriMCP-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "launchclaude"; Description: "Offer to open Claude Desktop when the installer finishes"; GroupDescription: "After installing:"; Flags: unchecked
Name: "dockerlogontask"; Description: "Register a log-on task to run: docker compose for the bundled multi-user stack file (requires Docker in PATH)"; GroupDescription: "Local / Docker:"; Flags: unchecked

[Files]
Source: "trimcp-launch.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\wheels\*"; DestDir: "{app}\wheels"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\docker-compose.yml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\Caddyfile"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\trimcp\*"; DestDir: "{app}\trimcp"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\admin\*"; DestDir: "{app}\admin"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\deploy\multiuser\Dockerfile"; DestDir: "{app}\deploy\multiuser"; Flags: ignoreversion
Source: "..\..\deploy\multiuser\docker-compose.yml"; DestDir: "{app}\deploy\multiuser"; Flags: ignoreversion
Source: "..\..\deploy\compose.stack.env"; DestDir: "{app}\deploy"; Flags: ignoreversion    
Source: "..\..\*.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "scripts\*.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Dirs]
Name: "{userappdata}\TriMCP"
Name: "{userappdata}\TriMCP\logs"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\trimcp-launch.exe"

[Run]
Filename: "{app}\trimcp-launch.exe"; Description: "Start TriMCP launcher (stdio shim)"; Flags: nowait postinstall skipifsilent
Filename: "{localappdata}\Programs\Claude\Claude.exe"; Flags: shellexec runasoriginaluser nowait skipifsilent skipifdoesntexist; Tasks: launchclaude

[Code]
var
  ModePage: TInputOptionWizardPage;
  DockerPage: TOutputMsgWizardPage;
  ServerPage: TInputQueryWizardPage;
  CloudIntroPage: TOutputMsgWizardPage;
  CloudTenantPage: TInputQueryWizardPage;
  HardwarePage: TInputOptionWizardPage;
  BridgesPage: TInputOptionWizardPage;
  SilentModeStr: string;
  SilentServerAddr: string;
  SilentTenant: string;
  SilentBridges: string;
  SilentBackend: string;

function GetSilentParam(const Key: string): string;
begin
  Result := ExpandConstant('{param:' + Key + '|}');
end;

function IsSilentInstall: Boolean;
begin
  Result := WizardSilent or WizardVerySilent;
end;

procedure LoadSilentParams;
begin
  SilentModeStr := LowerCase(Trim(GetSilentParam('MODE')));
  SilentServerAddr := Trim(GetSilentParam('SERVERADDR'));
  SilentTenant := Trim(GetSilentParam('TENANT'));
  SilentBridges := Trim(GetSilentParam('BRIDGES'));
  SilentBackend := Trim(GetSilentParam('BACKEND'));
  if SilentBackend = '' then
    SilentBackend := 'auto';
  if SilentModeStr = '' then
    SilentModeStr := 'local';
  if (SilentModeStr = 'office') or (SilentModeStr = 'multi-user') or (SilentModeStr = 'multi_user') then
    SilentModeStr := 'multiuser';
end;

function InitializeSetup(): Boolean;
begin
  LoadSilentParams;
  Result := True;
end;

procedure InitializeWizard;
begin
  ModePage := CreateInputOptionPage(wpWelcome,
    'How will you use TriMCP?',
    'Pick the deployment path that matches your organization.',
    'Your choice determines how TriMCP stores data and connects to services.',
    True);
  ModePage.Add('Local — Just for me on this PC (Docker Desktop runs databases on localhost)');
  ModePage.Add('Office Shared — Connect to my company''s TriMCP server (multi-user)');
  ModePage.Add('Cloud — Connect to TriMCP hosted in Microsoft Azure');
  ModePage.Values[0] := True;

  DockerPage := CreateOutputMsgPage(ModePage.ID,
    'Docker Desktop',
    'Local mode needs Docker Desktop.',
    'Docker Desktop runs the local PostgreSQL, MongoDB, Redis, and MinIO stack.' + #13#10 + #13#10 +
    'If it is not installed yet, download it from https://www.docker.com/products/docker-desktop/ ' +
    'and start it before using TriMCP.' + #13#10 + #13#10 +
    'Click Next when you are ready to continue.');

  ServerPage := CreateInputQueryPage(DockerPage.ID,
    'Office Shared server',
    'Connection details from IT',
    'Paste the base URL for your TriMCP deployment (for example the HTTPS URL of the company gateway):');
  ServerPage.Add('Server URL:', False);

  CloudIntroPage := CreateOutputMsgPage(ServerPage.ID,
    'Cloud sign-in',
    'Microsoft 365 sign-in',
    'TriMCP will use Microsoft Entra ID (device code flow) the first time you start the launcher.' + #13#10 + #13#10 +
    'You will complete sign-in in the browser when prompted.');

  CloudTenantPage := CreateInputQueryPage(CloudIntroPage.ID,
    'Azure AD tenant',
    'Tenant identifier',
    'Enter your organization''s tenant domain or ID (example: contoso.onmicrosoft.com):');
  CloudTenantPage.Add('Tenant:', False);

  HardwarePage := CreateInputOptionPage(CloudTenantPage.ID,
    'Hardware acceleration',
    'Inference backend',
    'TriMCP can use your GPU or NPU when available. Pick a fallback if auto-detection is wrong.',
    True);
  HardwarePage.Add('Recommended — auto-detect (best available)');
  HardwarePage.Add('CPU only');
  HardwarePage.Add('NVIDIA CUDA');
  HardwarePage.Add('AMD ROCm');
  HardwarePage.Add('Intel OpenVINO / NPU');
  HardwarePage.Add('Apple Metal (MPS)');
  HardwarePage.Values[0] := True;

  BridgesPage := CreateInputOptionPage(HardwarePage.ID,
    'Document bridges (optional)',
    'Connect document sources',
    'You can enable these now or configure them later. OAuth sign-in runs when you first use each bridge.',
    False);
  BridgesPage.Add('Microsoft SharePoint / OneDrive (Graph)');
  BridgesPage.Add('Google Drive');
  BridgesPage.Add('Dropbox');
end;

function ModeIndex: Integer;
begin
  if IsSilentInstall then
  begin
    if SilentModeStr = 'multiuser' then Result := 1
    else if SilentModeStr = 'cloud' then Result := 2
    else Result := 0;
  end
  else
    Result := ModePage.SelectedValueIndex;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if IsSilentInstall then
  begin
    Result := True;
    Exit;
  end;

  if PageID = DockerPage.ID then
    Result := ModeIndex <> 0;

  if PageID = ServerPage.ID then
    Result := ModeIndex <> 1;

  if PageID = CloudIntroPage.ID then
    Result := ModeIndex <> 2;

  if PageID = CloudTenantPage.ID then
    Result := ModeIndex <> 2;
end;

function NextButtonClick(CurPageID: Integer; var NextPageID: Integer): Boolean;
begin
  Result := True;
  if IsSilentInstall then Exit;

  if CurPageID = ServerPage.ID then
  begin
    if ModeIndex = 1 then
    begin
      if Trim(ServerPage.Values[0]) = '' then
      begin
        MsgBox('Enter the server URL supplied by IT, then click Next.', mbError, MB_OK);
        Result := False;
      end;
    end;
  end;

  if CurPageID = CloudTenantPage.ID then
  begin
    if ModeIndex = 2 then
    begin
      if Trim(CloudTenantPage.Values[0]) = '' then
      begin
        MsgBox('Enter your Microsoft 365 / Entra tenant domain.', mbError, MB_OK);
        Result := False;
      end;
    end;
  end;
end;

function HardwareBackend: string;
begin
  if IsSilentInstall then
  begin
    Result := SilentBackend;
    Exit;
  end;
  case HardwarePage.SelectedValueIndex of
    0: Result := 'auto';
    1: Result := 'cpu';
    2: Result := 'cuda';
    3: Result := 'rocm';
    4: Result := 'openvino_npu';
    5: Result := 'mps';
  else
    Result := 'auto';
  end;
end;

function BridgesList: string;
var
  parts: string;
begin
  if IsSilentInstall then
  begin
    Result := SilentBridges;
    Exit;
  end;
  parts := '';
  if BridgesPage.Values[0] then
  begin
    if parts <> '' then parts := parts + ',';
    parts := parts + 'sharepoint';
  end;
  if BridgesPage.Values[1] then
  begin
    if parts <> '' then parts := parts + ',';
    parts := parts + 'gdrive';
  end;
  if BridgesPage.Values[2] then
  begin
    if parts <> '' then parts := parts + ',';
    parts := parts + 'dropbox';
  end;
  Result := parts;
end;

function ModeFileValue: string;
begin
  case ModeIndex of
    0: Result := 'local';
    1: Result := 'multiuser';
    2: Result := 'cloud';
  else
    Result := 'local';
  end;
end;

procedure WriteCloudBridgesJson(const AppDataDir, Tenant: string);
var
  lines: TArrayOfString;
  t: string;
begin
  if Trim(Tenant) = '' then Exit;
  { Minimal escape for double quotes in tenant string }
  t := Tenant;
  StringChangeEx(t, '\', '\\', True);
  StringChangeEx(t, '"', '\"', True);
  SetArrayLength(lines, 8);
  lines[0] := '{';
  lines[1] := '  "cloud": {';
  lines[2] := '    "tenant_id": "' + t + '",';
  lines[3] := '    "client_id": "",';
  lines[4] := '    "scopes": ["https://graph.microsoft.com/User.Read"],';
  lines[5] := '    "msal_cache_file": "msal_cache.bin"';
  lines[6] := '  }';
  lines[7] := '}';
  SaveStringsToFile(AppDataDir + '\bridges.json', lines, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDataDir, modeStr, serverUrl, tenant, bridges, backend: string;
  envLines: TArrayOfString;
  composePath: string;
  schExit: Integer;
begin
  if CurStep <> ssPostInstall then Exit;

  AppDataDir := ExpandConstant('{userappdata}\TriMCP');
  LoadSilentParams;

  if IsSilentInstall then
  begin
    modeStr := SilentModeStr;
    serverUrl := SilentServerAddr;
    tenant := SilentTenant;
    bridges := SilentBridges;
    backend := SilentBackend;
  end
  else
  begin
    modeStr := ModeFileValue;
    serverUrl := ServerPage.Values[0];
    tenant := CloudTenantPage.Values[0];
    bridges := BridgesList;
    backend := HardwareBackend;
  end;

  if modeStr = 'office' then modeStr := 'multiuser';

  SaveStringToFile(AppDataDir + '\mode.txt', modeStr, False);

  SetArrayLength(envLines, 6);
  envLines[0] := 'TRIMCP_MODE=' + modeStr;
  envLines[1] := 'TRIMCP_SERVER_URL=' + serverUrl;
  envLines[2] := 'TRIMCP_TENANT=' + tenant;
  envLines[3] := 'TRIMCP_BRIDGES=' + bridges;
  envLines[4] := 'TRIMCP_BACKEND=' + backend;
  envLines[5] := 'TRIMCP_APP_ROOT=' + ExpandConstant('{app}');
  SaveStringsToFile(AppDataDir + '\.env', envLines, False);

  if modeStr = 'cloud' then
    WriteCloudBridgesJson(AppDataDir, tenant);

  { Patch IDE config }
  Exec('powershell.exe',
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\Patch-IDEConfig.ps1') + '" -AppRoot "' + ExpandConstant('{app}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, schExit);

  if (not IsSilentInstall) and (modeStr = 'local') and WizardIsTaskSelected('dockerlogontask') then
  begin
    composePath := ExpandConstant('{app}\docker-compose.yml');
    if FileExists(composePath) then
      Exec('schtasks.exe',
        '/Create /F /TN "TriMCP Local Stack" /TR "cmd.exe /c docker compose -f \"' + composePath + '\" up -d" /SC ONLOGON /RL LIMITED',
        '', SW_HIDE, ewWaitUntilTerminated, schExit);
  end;
end;
