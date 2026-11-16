#ifndef AppVersion
  #error AppVersion must be supplied by packaging/build.ps1
#endif
#define AppName "图纸审查"

[Setup]
AppId={{D3774241-9292-4C56-9BF5-4D46B795AB99}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=DrawCheck
DefaultDirName={localappdata}\Programs\DrawCheck
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19045
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableFinishedPage=no
UsePreviousAppDir=no
UsePreviousTasks=yes
UninstallDisplayIcon={app}\DrawCheck.exe
OutputDir=dist
OutputBaseFilename=DrawCheck-{#AppVersion}-win-x64-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
WizardStyle=modern

[Files]
Source: "dist\DrawCheck\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "stage\third_party\vendor\webview2\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\DrawCheck.exe"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\DrawCheck.exe"

[Run]
Filename: "{app}\DrawCheck.exe"; Description: "打开图纸审查"; Flags: nowait postinstall skipifsilent

[Messages]
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ReadyLabel1=准备安装图纸审查
FinishedHeadingLabel=图纸审查已安装
FinishedLabelNoIcons=单击“完成”打开图纸审查。

[Code]
const
  WebViewKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function InitializeUninstall: Boolean;
begin
  Result := FindWindowByWindowName('图纸审查') = 0;
  if not Result then
    MsgBox('请先关闭图纸审查，再卸载。', mbInformation, MB_OK);
end;

function HasWebView2: Boolean;
var Version: String;
begin
  Result := (RegQueryStringValue(HKCU, WebViewKey, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM32, WebViewKey, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Result := '';
  if not HasWebView2 then begin
    ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),
      '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := '无法安装内置 WebView2 运行时，请联系维护人员。'
    else if (Code <> 0) or not HasWebView2 then
      Result := 'WebView2 运行时安装未完成，请联系维护人员。错误码：' + IntToStr(Code);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then begin
    { Silent uninstall ALWAYS retains data. A normal uninstall defaults to Yes. }
    if not UninstallSilent then begin
      if MsgBox('是否保留审查项目数据？' + #13#10 +
        '选择“是”保留项目、原图副本、导出文件和设置；以后重新安装可继续使用。' + #13#10 +
        '选择“否”将永久删除默认数据目录。另行指定的数据目录和 Windows 凭据不受影响。',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDNO then begin
        if MsgBox('确定永久删除默认目录中的全部审查数据？此操作无法撤销。',
          mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then begin
          DataDir := ExpandConstant('{localappdata}\DrawCheck');
          if not DelTree(DataDir, True, True, True) then
            MsgBox('部分数据未能删除，请关闭程序后检查：' + DataDir, mbError, MB_OK);
        end;
      end;
    end;
  end;
end;
