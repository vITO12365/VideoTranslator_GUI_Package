[CmdletBinding()]
param(
    [string]$OutputRoot = (Join-Path $PWD 'PujjoAudioInfo')
)

$ErrorActionPreference = 'Continue'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$resultDir = Join-Path $OutputRoot $stamp
New-Item -ItemType Directory -Force -Path $resultDir | Out-Null

function Save-Text {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [scriptblock]$Action
    )

    $path = Join-Path $resultDir $Name
    try {
        & $Action 2>&1 | Out-String -Width 4096 | Set-Content -Encoding UTF8 $path
    }
    catch {
        "ERROR: $($_.Exception.Message)" | Set-Content -Encoding UTF8 $path
    }
}

Save-Text 'README.txt' {
    @"
PUJJO Windows audio information bundle
Created: $(Get-Date -Format o)

This script performs read-only system queries. It does not install a driver,
change the registry, enable test mode, or write hardware registers.
"@
}

Save-Text 'windows-version.txt' {
    Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion,
        OsName, OsVersion, OsBuildNumber, OsArchitecture,
        BiosFirmwareType, BiosManufacturer, BiosName, BiosVersion,
        CsManufacturer, CsModel, CsSystemFamily, CsSystemType,
        DeviceGuardSmartStatus, DeviceGuardRequiredSecurityProperties,
        DeviceGuardAvailableSecurityProperties | Format-List
}

Save-Text 'smbios.txt' {
    'Win32_ComputerSystemProduct'
    Get-CimInstance Win32_ComputerSystemProduct | Format-List *
    'Win32_BaseBoard'
    Get-CimInstance Win32_BaseBoard | Format-List *
    'Win32_BIOS'
    Get-CimInstance Win32_BIOS | Format-List *
}

Save-Text 'secure-boot.txt' {
    try {
        "SecureBoot=$((Confirm-SecureBootUEFI))"
    }
    catch {
        "Secure Boot state unavailable: $($_.Exception.Message)"
    }
}

Save-Text 'pnp-audio-summary.txt' {
    Get-PnpDevice -PresentOnly | Where-Object {
        $_.InstanceId -match '10EC5682|RTL5682|MX98360A|RTL1019|VEN_8086&DEV_54C8' -or
        $_.Class -match 'MEDIA|AudioEndpoint|System'
    } | Sort-Object Class, FriendlyName | Format-Table -AutoSize Status, Class, FriendlyName, InstanceId, Problem
}

Save-Text 'pnp-problem-devices.txt' {
    Get-PnpDevice | Where-Object Status -ne 'OK' |
        Sort-Object Class, FriendlyName | Format-Table -AutoSize Status, Class, FriendlyName, InstanceId, Problem
}

$targetDevices = Get-PnpDevice | Where-Object {
    $_.InstanceId -match '10EC5682|RTL5682|MX98360A|RTL1019|VEN_8086&DEV_54C8'
}

foreach ($device in $targetDevices) {
    $safeName = ($device.InstanceId -replace '[\\/:*?"<>|&]', '_')
    Save-Text ("device-$safeName.txt") {
        "Status: $($device.Status)"
        "Class: $($device.Class)"
        "FriendlyName: $($device.FriendlyName)"
        "InstanceId: $($device.InstanceId)"
        "Problem: $($device.Problem)"
        ''
        Get-PnpDeviceProperty -InstanceId $device.InstanceId |
            Sort-Object KeyName | Format-Table -Wrap -AutoSize KeyName, Type, Data
    }
}

Save-Text 'pnputil-connected-deviceids.txt' {
    & pnputil.exe /enum-devices /connected /deviceids
}

Save-Text 'pnputil-problem-deviceids.txt' {
    & pnputil.exe /enum-devices /problem /deviceids
}

Save-Text 'pnputil-drivers.txt' {
    & pnputil.exe /enum-drivers
}

Save-Text 'signed-pnp-drivers.txt' {
    Get-CimInstance Win32_PnPSignedDriver | Where-Object {
        $_.DeviceID -match '10EC5682|RTL5682|MX98360A|RTL1019|VEN_8086&DEV_54C8' -or
        $_.DeviceClass -match 'MEDIA|System'
    } | Sort-Object DeviceName | Format-List DeviceName, DeviceID, DeviceClass,
        Manufacturer, DriverProviderName, DriverVersion, DriverDate, InfName,
        IsSigned, Signer
}

Save-Text 'registry-rt5682.txt' {
    & reg.exe query 'HKLM\SYSTEM\CurrentControlSet\Enum\ACPI\10EC5682' /s
}

Save-Text 'registry-audio-controller.txt' {
    & reg.exe query 'HKLM\SYSTEM\CurrentControlSet\Enum\PCI' /f 'VEN_8086&DEV_54C8' /s
}

$setupApi = Join-Path $env:windir 'INF\setupapi.dev.log'
if (Test-Path $setupApi) {
    Copy-Item -LiteralPath $setupApi -Destination (Join-Path $resultDir 'setupapi.dev.log')
}

Save-Text 'system-events-last-14-days.txt' {
    $start = (Get-Date).AddDays(-14)
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $start } |
        Where-Object {
            $_.ProviderName -match 'Kernel-PnP|DriverFrameworks|ACPI|Audio|HDA|Intel|WHEA' -or
            $_.Message -match '10EC5682|RTL5682|MX98360A|RTL1019|54C8|audio|Smart Sound|SST'
        } | Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
        Format-List
}

$acpiDump = Get-Command acpidump.exe -ErrorAction SilentlyContinue
$iasl = Get-Command iasl.exe -ErrorAction SilentlyContinue
if ($acpiDump) {
    $acpiDir = Join-Path $resultDir 'acpi'
    New-Item -ItemType Directory -Force -Path $acpiDir | Out-Null
    Push-Location $acpiDir
    try {
        & $acpiDump.Source -b -z 2>&1 | Out-String -Width 4096 |
            Set-Content -Encoding UTF8 (Join-Path $acpiDir 'acpidump-output.txt')
        if ($iasl) {
            Get-ChildItem -File -Filter '*.dat' | ForEach-Object {
                & $iasl.Source -d $_.FullName 2>&1 | Out-String -Width 4096 |
                    Add-Content -Encoding UTF8 (Join-Path $acpiDir 'iasl-output.txt')
            }
        }
    }
    finally {
        Pop-Location
    }
}
else {
    'acpidump.exe was not found in PATH. Install the public ACPICA tools and rerun as Administrator.' |
        Set-Content -Encoding UTF8 (Join-Path $resultDir 'acpi-not-collected.txt')
}

$zipPath = "$resultDir.zip"
Compress-Archive -Path (Join-Path $resultDir '*') -DestinationPath $zipPath -Force

Write-Host "Collection complete."
Write-Host "Folder: $resultDir"
Write-Host "Archive: $zipPath"
