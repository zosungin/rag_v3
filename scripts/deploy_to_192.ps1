# Zabbix RAG — 192.168.6.24 운영 반영 + rag-fastapi 재시작
# 사용: 저장소 루트에서  .\scripts\deploy_to_192.ps1
# 요구: OpenSSH 클라이언트(scp, ssh), root SSH 키

$ErrorActionPreference = "Stop"
$Server = "192.168.6.24"
$RemoteUser = "root"
$RemotePath = "/app/rag/fastapi"
$Container = "rag-fastapi"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Tarball = Join-Path $env:TEMP "rag_deploy_$(Get-Date -Format 'yyyyMMddHHmmss').tgz"

$Key = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
$SshOpts = @()
if (Test-Path $Key) {
    $SshOpts = @("-i", $Key, "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=30")
}

Set-Location $RepoRoot
Write-Host "Packaging $RepoRoot -> $Tarball"
tar -czf $Tarball --exclude=.git --exclude=__pycache__ --exclude=.venv --exclude=.cursor --exclude="*.pyc" .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Uploading to ${RemoteUser}@${Server}:/tmp/"
& scp @SshOpts $Tarball "${RemoteUser}@${Server}:/tmp/rag_deploy.tgz"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Extract + restart container"
$bash = "cd $RemotePath && tar -xzf /tmp/rag_deploy.tgz && rm -f /tmp/rag_deploy.tgz && docker restart $Container && sleep 2 && docker ps --filter name=$Container"
& ssh @SshOpts "${RemoteUser}@${Server}" $bash

Remove-Item -Force $Tarball -ErrorAction SilentlyContinue
Write-Host "Done."
