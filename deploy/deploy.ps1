#requires -Version 5.1
# Public workstation entry point; see deploy/README.md and docs/deployment.md.
# Default updates preserve data/configuration; -ResetDatabase is an explicit destructive test reset.
# -PackageOnly writes a transport payload without SSH. Keep that output under ongoing_work.

<#
.SYNOPSIS
Deploy da API LEM por SSH, a partir de Windows PowerShell ou PowerShell 7.
.EXAMPLE
.\deploy\deploy.ps1
.EXAMPLE
.\deploy\deploy.ps1 -ResetDatabase
.EXAMPLE
.\deploy\deploy.ps1 -ServerHost lem.rodrod.pt -UserName lem -KeyPath "$env:USERPROFILE\.ssh\id_ed25519"
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$ServerHost = 'lem.rodrod.pt',
    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]*$')]
    [string]$UserName = 'lem',
    [ValidateRange(1,65535)]
    [int]$Port = 22,
    [string]$KeyPath,
    [string]$RemotePath = 'auto',
    [ValidatePattern('^$|^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$')]
    [string]$Domain = '',
    [string]$ProjectPath = '',
    [switch]$ResetDatabase,
    # Packaging/inspection without SSH; useful for testing the deploy package.
    [string]$PackageOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Resolve defaults in the script body: Windows PowerShell can evaluate parameter
# defaults before $PSScriptRoot is populated. Never infer the project from cwd.
if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
    $scriptFile = $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($scriptFile)) {
        throw 'Nao foi possivel localizar deploy.ps1. Execute o ficheiro com -File ou indique -ProjectPath.'
    }
    $ProjectPath = Split-Path -Parent (Split-Path -Parent $scriptFile)
}

function Invoke-NativeChecked {
    param([string]$Executable,[string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "O comando $Executable terminou com codigo $LASTEXITCODE." }
}

$projectRoot = (Resolve-Path -LiteralPath $ProjectPath).Path
foreach ($relative in @('server/run.py','server/requirements.txt','shared/checklist-v1.json','deploy/compose.yml','deploy/remote-deploy.sh','deploy/find-installation.py','deploy/prepare-config.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relative) -PathType Leaf)) { throw "Projeto incompleto: $relative" }
}
if ([string]::IsNullOrWhiteSpace($RemotePath) -or $RemotePath.Contains("`n") -or $RemotePath.Contains("`r")) { throw 'RemotePath invalido.' }
if ($RemotePath.StartsWith('~/')) { $RemotePath = $RemotePath.Substring(2) }
if ($KeyPath) { $KeyPath = (Resolve-Path -LiteralPath $KeyPath).Path }

$releaseId = [Guid]::NewGuid().ToString('N')
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('lem-deploy-' + $releaseId)
$stage = Join-Path $tempRoot 'source'
$archive = Join-Path $tempRoot 'payload.zip'
$remoteStage = '.cache/lem-deploy-' + $releaseId
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try {
    foreach ($directory in @('server','shared','deploy')) {
        $base = Join-Path $projectRoot $directory
        foreach ($file in Get-ChildItem -LiteralPath $base -File -Recurse -Force) {
            $relative = $file.FullName.Substring($projectRoot.Length).TrimStart([char[]]@('\','/'))
            $parts = @($relative -split '[\\/]')
            $skip = $false
            foreach ($part in $parts) {
                if ($part -in @('__pycache__','.pytest_cache','.venv','venv','build','backups','.deploy-backups','.git','node_modules','secrets','.ssh')) { $skip=$true; break }
            }
            if ($skip -or $file.Name -eq '.env' -or ($file.Name.StartsWith('.env.') -and $file.Name -ne '.env.example') -or $file.Extension -in @('.pyc','.db','.dump','.pem','.key','.pfx','.p12','.zip','.tar','.gz') -or $file.Name -match '\.(db|sqlite|sqlite3)(-|$)') { continue }
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Link simbolico/reparse point nao permitido no pacote: $relative" }
            $destination = Join-Path $stage $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            if ($file.Extension -eq '.sh') {
                $content = [IO.File]::ReadAllText($file.FullName).Replace("`r`n","`n").Replace("`r","`n")
                [IO.File]::WriteAllText($destination,$content,$utf8NoBom)
            } else { [IO.File]::Copy($file.FullName,$destination,$true) }
        }
    }
    $dockerIgnore = Join-Path $projectRoot '.dockerignore'
    if (Test-Path -LiteralPath $dockerIgnore) { [IO.File]::Copy($dockerIgnore,(Join-Path $stage '.dockerignore'),$true) }
    # Windows PowerShell 5.1 does not preload the core compression assembly.
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    # Windows PowerShell/.NET Framework may emit backslashes from
    # CreateFromDirectory. Write portable ZIP entry names explicitly instead.
    $zip = [IO.Compression.ZipFile]::Open($archive,[IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in Get-ChildItem -LiteralPath $stage -File -Recurse -Force) {
            $entryName = $file.FullName.Substring($stage.Length).TrimStart([char[]]@('\','/')).Replace('\','/')
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$file.FullName,$entryName,[IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    } finally { $zip.Dispose() }
    $archiveSha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($PackageOnly) {
        $output = [IO.Path]::GetFullPath($PackageOnly)
        if ([IO.Path]::GetFullPath($output) -eq [IO.Path]::GetFullPath($archive)) { throw 'Escolha um destino fora da pasta temporaria.' }
        [IO.File]::Copy($archive,$output,$true)
        Write-Host "Pacote criado sem ligacao ao servidor: $output"
        Write-Host "SHA256: $archiveSha"
        return
    }
    $ssh = (Get-Command ssh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $scp = (Get-Command scp -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $sshArgs = @('-o','ConnectTimeout=15','-o','ServerAliveInterval=20','-o','ServerAliveCountMax=3','-p',"$Port")
    $scpArgs = @('-o','ConnectTimeout=15','-P',"$Port")
    if ($KeyPath) { $sshArgs += @('-i',$KeyPath); $scpArgs += @('-i',$KeyPath) }
    $target = $UserName + '@' + $ServerHost
    if ($ResetDatabase) { Write-Host 'MODO RESET: os dados da base de testes serao apagados; .env e certificados serao conservados.' -ForegroundColor Yellow }
    else { Write-Host 'MODO ATUALIZACAO: dados conservados e backup automatico antes de substituir o codigo.' }
    Write-Host "Destino SSH: $target | Pasta: $RemotePath"
    # The command contains only fixed tokens and a generated safe directory name.
    Invoke-NativeChecked $ssh ($sshArgs + @($target,"command -v python3 >/dev/null && command -v docker >/dev/null && umask 077 && mkdir -p $remoteStage"))
    Invoke-NativeChecked $scp ($scpArgs + @($archive,($target + ':' + $remoteStage + '/payload.zip')))
    # JSON is carried as base64 into Python, never interpolated as shell syntax.
    $config = @{
        remote_path=$RemotePath; reset=[bool]$ResetDatabase; release=$releaseId
        domain=$Domain; archive_sha=$archiveSha; transfer_dir=$remoteStage
    } | ConvertTo-Json -Compress
    $config64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($config))
    $bootstrap = @'
import base64,hashlib,json,os,shutil,stat,sys,zipfile
from pathlib import Path,PurePosixPath
c=json.loads(base64.b64decode("__CONFIG__"))
staging=Path.home()/c['transfer_dir']
archive=staging/'payload.zip'
if hashlib.sha256(archive.read_bytes()).hexdigest()!=c['archive_sha']:
    raise SystemExit('SHA256 do pacote nao corresponde; deploy cancelado')
source=staging/'source'
source.mkdir(mode=0o700)
with zipfile.ZipFile(archive) as z:
    if sum(i.file_size for i in z.infolist())>512*1024*1024:
        raise SystemExit('Pacote demasiado grande')
    entries=[];seen=set()
    for i in z.infolist():
        # Accept historical Windows ZIP separators only after normalization;
        # traversal, absolute paths, symlinks and private files remain forbidden.
        name=i.filename.replace("\\","/")
        p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or not p.parts:
            raise SystemExit('Caminho inseguro no pacote: '+repr(i.filename))
        if p.parts[0] not in ('server','shared','deploy','.dockerignore'):
            raise SystemExit('Ficheiro fora das pastas de deployment: '+repr(i.filename))
        if p.parts[0]=='.dockerignore' and len(p.parts)!=1:
            raise SystemExit('Caminho invalido para .dockerignore')
        if '.env' in p.parts or 'backups' in p.parts or stat.S_ISLNK(i.external_attr>>16):
            raise SystemExit('Configuracao privada/link nao permitido no pacote')
        canonical=p.as_posix()
        if canonical in seen:raise SystemExit('Caminho duplicado no pacote: '+canonical)
        seen.add(canonical);entries.append((i,p,name.endswith('/')))
    # Do not call extractall on the original Windows names. Extract the exact
    # normalized and validated paths used above.
    for info,path,is_dir in entries:
        destination=source.joinpath(*path.parts)
        if is_dir:destination.mkdir(parents=True,exist_ok=True)
        else:
            destination.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as src,destination.open('xb') as dst:shutil.copyfileobj(src,dst)
# ZIP extraction does not retain Unix modes. Set runtime-readable source modes
# for Docker COPY + non-root API, while the transfer parent remains private.
for path in source.rglob('*'):
    path.chmod(0o755 if path.is_dir() or path.suffix=='.sh' else 0o644)
import runpy
resolver=runpy.run_path(str(source/'deploy/find-installation.py'))['resolve_target']
target=resolver(c['remote_path'],Path.home(),c['domain'])
os.execv('/bin/bash',['bash',str(source/'deploy/remote-deploy.sh'),str(target),'1' if c['reset'] else '0',c['release'],c['domain']])
'@
    $bootstrap = $bootstrap.Replace('__CONFIG__',$config64).Replace("`r`n","`n")
    $bootstrap64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
    # A remote PTY lets sudo prompt on the server. No sudo/key password is
    # collected, cached or passed by this script.
    $remoteCommand = "printf %s $bootstrap64 | base64 -d > $remoteStage/bootstrap.py && python3 $remoteStage/bootstrap.py"
    Invoke-NativeChecked $ssh ($sshArgs + @('-tt',$target,$remoteCommand))
    Invoke-NativeChecked $ssh ($sshArgs + @($target,"rm -rf -- $remoteStage"))
    Write-Host 'Deploy terminado. Este script atualiza o servidor; a app Qt deve ser recompilada separadamente.' -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
