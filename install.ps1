#!/usr/bin/env pwsh
param(
    [string]$Version = "latest"
)

$Repo = "Aniket-think41/skillshare-mcp"

if ($Version -eq "latest") {
    $Url = "https://github.com/$Repo/releases/latest/download/skillshare-mcp-windows-amd64.exe"
} else {
    $Url = "https://github.com/$Repo/releases/download/$Version/skillshare-mcp-windows-amd64.exe"
}

$Out = "$env:TEMP\skillshare-mcp.exe"
Write-Host "Downloading skillshare-mcp for Windows..."
Invoke-WebRequest -Uri $Url -OutFile $Out

$InstallDir = "$env:USERPROFILE\.skillshare\bin"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Move-Item -Force $Out "$InstallDir\skillshare-mcp.exe"

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$InstallDir", "User")
    $env:Path += ";$InstallDir"
}

Write-Host "Installed skillshare-mcp to $InstallDir\skillshare-mcp.exe"
Write-Host "You may need to restart your terminal for PATH changes to take effect."
