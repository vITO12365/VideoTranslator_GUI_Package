[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ToolsDirectory,

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory = (Join-Path -Path $env:USERPROFILE -ChildPath ("Desktop\Pujjo-ACPI-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ToolsDirectory -PathType Container)) {
    throw "ToolsDirectory does not exist or is not a directory: $ToolsDirectory"
}

$resolvedTools = (Resolve-Path -LiteralPath $ToolsDirectory).Path
$acpiDump = Join-Path -Path $resolvedTools -ChildPath 'acpidump.exe'
$iasl = Join-Path -Path $resolvedTools -ChildPath 'iasl.exe'

if (-not (Test-Path -LiteralPath $acpiDump -PathType Leaf)) {
    throw "acpidump.exe was not found in ToolsDirectory: $resolvedTools"
}

if (-not (Test-Path -LiteralPath $iasl -PathType Leaf)) {
    throw "iasl.exe was not found in ToolsDirectory: $resolvedTools"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
$locationWasPushed = $false

try {
    Push-Location -LiteralPath $resolvedOutput
    $locationWasPushed = $true

    & $acpiDump -b -z
    if ($LASTEXITCODE -ne 0) {
        throw "acpidump.exe failed with exit code $LASTEXITCODE. Run PowerShell as Administrator."
    }

    $tables = @(Get-ChildItem -LiteralPath $resolvedOutput -Filter '*.dat' -File)
    if ($tables.Count -eq 0) {
        throw 'acpidump.exe did not create any .dat ACPI table files.'
    }

    foreach ($table in $tables) {
        & $iasl -d $table.FullName
        if ($LASTEXITCODE -ne 0) {
            Write-Warning ("iasl.exe could not decompile {0}; the original .dat file will still be included." -f $table.Name)
        }
    }
}
finally {
    if ($locationWasPushed) {
        Pop-Location
    }
}

$zipPath = $resolvedOutput + '.zip'
$filesToArchive = @(Get-ChildItem -LiteralPath $resolvedOutput -Force)

if ($filesToArchive.Count -eq 0) {
    throw "The output directory is empty: $resolvedOutput"
}

Compress-Archive -Path (Join-Path -Path $resolvedOutput -ChildPath '*') -DestinationPath $zipPath -Force
Write-Host "ACPI collection complete: $zipPath"
