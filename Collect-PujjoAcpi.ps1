[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$ToolsDirectory,

    [string]$OutputDirectory = (Join-Path $env:USERPROFILE ("Desktop\Pujjo-ACPI-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

$ErrorActionPreference = 'Stop'

$acpiDump = Join-Path $ToolsDirectory 'acpidump.exe'
$iasl = Join-Path $ToolsDirectory 'iasl.exe'

if (-not (Test-Path -LiteralPath $acpiDump -PathType Leaf)) {
    throw "找不到 $acpiDump；請把 ToolsDirectory 指到含 acpidump.exe 的資料夾。"
}
if (-not (Test-Path -LiteralPath $iasl -PathType Leaf)) {
    throw "找不到 $iasl；請把 ToolsDirectory 指到含 iasl.exe 的資料夾。"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path

Push-Location $resolvedOutput
try {
    & $acpiDump -b -z
    if ($LASTEXITCODE -ne 0) {
        throw "acpidump 失敗，exit code=$LASTEXITCODE。請確認 PowerShell 是以系統管理員身分執行。"
    }

    $tables = Get-ChildItem -LiteralPath $resolvedOutput -Filter '*.dat' -File
    if ($tables.Count -eq 0) {
        throw 'acpidump 沒有產生 .dat ACPI table。'
    }

    foreach ($table in $tables) {
        & $iasl -d $table.FullName
    }
}
finally {
    Pop-Location
}

$zipPath = "$resolvedOutput.zip"
Compress-Archive -Path (Join-Path $resolvedOutput '*') -DestinationPath $zipPath -Force
Write-Host "完成：$zipPath"
