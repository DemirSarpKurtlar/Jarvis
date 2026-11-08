; JARVIS kurulum programı (Inno Setup 6)
; Not: Bu dosya UTF-8 BOM ile kaydedilmeli (Türkçe karakterler için); BOM'u silmeyin.
;
; Derleme: packaging\build.ps1 (ya da CI). Elle derlemek için:
;   ISCC /DAppVersion=1.0.0.dev1 /DAppNumericVersion=1.0.0.0 ^
;        /DSourceDir=<depo>\dist\JARVIS /DOutputDir=<depo>\dist\installer packaging\jarvis.iss
;
; * Yönetici izni istemez; kullanıcıya özel kurulur: %LOCALAPPDATA%\Programs\JARVIS
; * Sabit AppId: yeni sürüm eskisinin üzerine temizce kurulur (yükseltme)
; * Kaldırma: program dosyalarını ve "Windows açılınca başlat" kaydını siler.
;   Kullanıcı verisi (ayarlar, konuşma geçmişi, loglar, API anahtarı) yalnızca
;   kullanıcı "Evet" derse silinir. Sessiz kaldırmada /DELETEUSERDATA=1 ile silinir.
;   Veri konumları:
;     %APPDATA%\JARVIS          ayarlar (settings.json)
;     %LOCALAPPDATA%\JARVIS     veritabanı (konuşmalar, kullanım), loglar (Logs\)
;     Windows Credential Manager: "JARVIS" kaydı (Claude API anahtarı)

#ifndef AppVersion
  #error "AppVersion tanımlı değil. packaging\build.ps1 ile derleyin ya da ISCC'ye /DAppVersion=... verin."
#endif
#ifndef AppNumericVersion
  #define AppNumericVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\JARVIS"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

#define AppName "JARVIS"
#define AppExeName "JARVIS.exe"
; jarvis/app.py WINDOWS_APP_ID ile aynı olmalı (görev çubuğu gruplaması)
#define AppUserModelID "JARVIS.Asistan"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define ApprovedKey "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

[Setup]
; Bu GUID asla değiştirilmemeli: yükseltmeler ve kaldırma bu kimlikle eşleşir
AppId={{7D3E5B21-9C4F-4A86-B1E2-5F08C6A9D417}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppNumericVersion}
VersionInfoProductVersion={#AppNumericVersion}
VersionInfoDescription={#AppName} kurulum programı
; Yönetici izni olmadan, yalnızca bu kullanıcı için kur
PrivilegesRequired=lowest
; Yönetici izni olmayan kurulumda {autopf} = %LOCALAPPDATA%\Programs
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=JARVIS-Kurulum-{#AppVersion}
SetupIconFile=..\jarvis\assets\icon\jarvis.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Çalışan JARVIS önce kendi "--kapat" komutuyla temizce kapatılır (bkz. [Code]);
; Windows Yeniden Başlatma Yöneticisi yalnızca yedek yol.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[CustomMessages]
turkish.AutostartTask=Windows açılınca JARVIS'i başlat
turkish.AutostartGroup=Başlangıç:
turkish.LaunchApp=JARVIS'i şimdi başlat
turkish.DeleteUserDataQuestion=JARVIS'in kişisel verileri de silinsin mi?%n%nAyarlar, konuşma geçmişi, kullanım kayıtları, loglar ve Windows Credential Manager'daki Claude API anahtarı.%n%nHayır dersen bunlar bilgisayarda kalır; JARVIS'i yeniden kurduğunda kaldığın yerden devam edersin.%n%nKonumlar:%n%1%n%2

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "{cm:AutostartTask}"; GroupDescription: "{cm:AutostartGroup}"; Flags: unchecked

[InstallDelete]
; Yükseltmede eski sürümden kalan kütüphaneler yenileriyle karışmasın
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Registry]
; jarvis/integrations/autostart.py ile aynı değer adı ve bayrak
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExeName}"" --baslangic"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Program klasörü boş kaldıysa onu da sil (kullanıcı verisi burada tutulmaz)
Type: dirifempty; Name: "{app}"

[Code]
var
  DeleteUserData: Boolean;

function ConfigDataDir(): String;
begin
  Result := ExpandConstant('{userappdata}\{#AppName}');
end;

function LocalDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

{ Çalışan JARVIS'e "temizce kapan" der ve kapanmasını bekler. }
procedure CloseRunningJarvis(const ExePath: String);
var
  ResultCode: Integer;
begin
  if FileExists(ExePath) then
  begin
    Log('Çalışan JARVIS kapatılıyor: ' + ExePath + ' --kapat');
    if not Exec(ExePath, '--kapat', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      Log('--kapat çalıştırılamadı')
    else
      Log('--kapat çıkış kodu: ' + IntToStr(ResultCode));
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Yükseltme: eski sürüm açıksa dosyaları değiştirmeden önce kapat }
  CloseRunningJarvis(ExpandConstant('{app}\{#AppExeName}'));
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseRunningJarvis(ExpandConstant('{app}\{#AppExeName}'));
  Result := True;
end;

procedure RemoveAutostartEntry();
begin
  { Kayıt kurulumda ya da JARVIS ayarlarından eklenmiş olabilir: her durumda sil }
  RegDeleteValue(HKEY_CURRENT_USER, '{#RunKey}', '{#AppName}');
  RegDeleteValue(HKEY_CURRENT_USER, '{#ApprovedKey}', '{#AppName}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if UninstallSilent() then
      DeleteUserData := ExpandConstant('{param:DELETEUSERDATA|0}') = '1'
    else
      DeleteUserData := MsgBox(FmtMessage(CustomMessage('DeleteUserDataQuestion'), [ConfigDataDir(), LocalDataDir()]),
                               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    Log('Kullanıcı verisi silinsin mi: ' + IntToStr(Ord(DeleteUserData)));
    { Anahtar Credential Manager'da: exe henüz silinmeden JARVIS'e sildir }
    if DeleteUserData then
      Exec(ExpandConstant('{app}\{#AppExeName}'), '--anahtari-sil', '', SW_HIDE,
           ewWaitUntilTerminated, ResultCode);
    RemoveAutostartEntry();
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if DeleteUserData then
    begin
      Log('Kullanıcı verisi siliniyor');
      DelTree(ConfigDataDir(), True, True, True);
      DelTree(LocalDataDir(), True, True, True);
    end;
  end;
end;
