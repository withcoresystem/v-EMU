# SPDX-License-Identifier: GPL-3.0-or-later
<#
.SYNOPSIS
    Apply a conservative performance profile to a Windows guest used by v-EMU.

.DESCRIPTION
    This is a guest-side helper, not part of the v-EMU host service. It never
    disables v-EMU plumbing (QEMU guest agent, virtio-serial, VirtioFS/WinFsp,
    USB, SPICE, balloon, vgpu3d) and it keeps Defender, the firewall, BitLocker,
    Windows Update and the Store enabled.

    Profiles:
      Safe      telemetry, diagnostics, consumer content and visual effects
      Balanced  Safe plus SysMain/WSearch and unused consumer services/tasks
      3D        Safe plus Game DVR/fullscreen-optimization tweaks

    Every profile also selects the High performance power plan, disables
    automatic sleep/hibernation/disk/monitor timeouts, turns off Fast Startup
    and disables USB selective suspend and PCIe ASPM.

.PARAMETER Profile
    Safe (default), Balanced or 3D. Balanced and 3D include the shared Safe
    settings. Run Balanced and then 3D to combine them.

.PARAMETER DryRun
    Print the plan without changing the guest. Safe to run on a non-Windows
    host for schema validation.

.PARAMETER LogPath
    Log file. Defaults to %TEMP%\vemu-guest-optimize.log.

.PARAMETER UserHive
    Optional path to another user's NTUSER.DAT. The Default profile hive is
    always updated; HKCU is updated when the script is not running as SYSTEM.

.PARAMETER SkipUserSettings
    Do not write HKCU or Default-profile values.

.PARAMETER SkipPower
    Do not change the power plan or sleep timeouts.

.EXAMPLE
    .\vemu-guest-optimize.ps1 -Profile Safe -DryRun

.EXAMPLE
    .\vemu-guest-optimize.ps1 -Profile Balanced
#>
[CmdletBinding()]
param(
    [ValidateSet('Safe', 'Balanced', '3D')]
    [string]$Profile = 'Safe',
    [switch]$DryRun,
    [string]$LogPath = '',
    [string]$UserHive,
    [switch]$SkipUserSettings,
    [switch]$SkipPower
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
if (-not $LogPath) {
    $LogPath = Join-Path ([IO.Path]::GetTempPath()) 'vemu-guest-optimize.log'
}

$script:LogLines = New-Object System.Collections.Generic.List[string]
function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '[{0}] {1} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $script:LogLines.Add($line)
    Write-Host $line
}

function Save-Log {
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path -Path $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        Set-Content -Path $LogPath -Value $script:LogLines -Encoding UTF8
    } catch {
        Write-Host ("WARN could not write log {0}: {1}" -f $LogPath, $_.Exception.Message)
    }
}

function Get-List {
    param($Common, $Extra)
    $items = @()
    if ($null -ne $Common) { $items += @($Common) }
    if ($null -ne $Extra) { $items += @($Extra) }
    return @($items | Where-Object { $null -ne $_ -and "$_" -ne '' })
}

$profilesPath = Join-Path -Path $PSScriptRoot -ChildPath 'profiles.json'
if (-not (Test-Path -Path $profilesPath)) {
    Write-Log "profiles.json not found next to the script: $profilesPath" 'ERROR'
    Save-Log
    exit 1
}
try {
    $data = Get-Content -Path $profilesPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Log "cannot parse profiles.json: $($_.Exception.Message)" 'ERROR'
    Save-Log
    exit 1
}

$profileData = $data.profiles.PSObject.Properties[$Profile].Value
if ($null -eq $profileData) {
    Write-Log "unknown profile: $Profile" 'ERROR'
    Save-Log
    exit 1
}

$protectedServices = @($data.protected.services)
$protectedPatterns = @($data.protected.patterns)
$services = Get-List $data.common.services $profileData.services
$tasks = Get-List $data.common.tasks $profileData.tasks
$hklm = Get-List $data.common.hklm $profileData.hklm
$hkcu = Get-List $data.common.hkcu $profileData.hkcu
$commands = @($data.common.commands)
$power = $data.common.power

function Test-ProtectedService {
    param([string]$Name)
    if ($protectedServices -contains $Name) { return $true }
    foreach ($pattern in $protectedPatterns) {
        if ($Name -like $pattern) { return $true }
    }
    return $false
}

Write-Log "v-EMU Windows guest optimization"
Write-Log "profile=$Profile dry-run=$([bool]$DryRun) host=$env:COMPUTERNAME user=$env:USERNAME"
Write-Log "planned: services=$($services.Count) tasks=$($tasks.Count) hklm=$($hklm.Count) hkcu=$($hkcu.Count) commands=$($commands.Count)"

if ($DryRun) {
    foreach ($name in $services) {
        if (Test-ProtectedService $name) {
            Write-Log "[dry-run] service $name -> SKIP (protected)"
        } else {
            Write-Log "[dry-run] service $name -> Disabled"
        }
    }
    foreach ($task in $tasks) {
        Write-Log "[dry-run] task $($task.path)$($task.name) -> Disabled"
    }
    foreach ($value in $hklm) {
        Write-Log "[dry-run] reg add $($value.key) /v $($value.name) /t $($value.type) /d $($value.data)"
    }
    foreach ($value in $hkcu) {
        Write-Log "[dry-run] user reg add <hive>\$($value.key) /v $($value.name) /t $($value.type) /d $($value.data)"
    }
    foreach ($command in $commands) {
        Write-Log "[dry-run] $($command.file) $($command.args -join ' ')"
    }
    if (-not $SkipPower) {
        Write-Log "[dry-run] powercfg /setactive $($power.activeScheme)"
        foreach ($pair in $power.change) {
            Write-Log "[dry-run] powercfg /change -$($pair[0]) $($pair[1])"
        }
        if ($power.hibernateOff) { Write-Log '[dry-run] powercfg /hibernate off' }
        if ($power.hiberbootOff) { Write-Log '[dry-run] HiberbootEnabled=0' }
        if ($power.usbSelectiveSuspendOff) { Write-Log '[dry-run] USB selective suspend off' }
        if ($power.pcieAspmOff) { Write-Log '[dry-run] PCIe ASPM off' }
    }
    Save-Log
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Log 'administrator rights are required' 'ERROR'
    Save-Log
    exit 1
}

function Set-RegValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Type,
        [Parameter(Mandatory = $true)][string]$Data
    )
    & reg.exe add $Key /v $Name /t $Type /d $Data /f 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "reg add failed: $Key :: $Name (exit $LASTEXITCODE)" 'WARN'
    }
}

function Disable-ServiceSafe {
    param([string]$Name)
    if (Test-ProtectedService $Name) {
        Write-Log "skip protected service: $Name" 'WARN'
        return
    }
    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $service) {
        Write-Log "service not found: $Name" 'WARN'
        return
    }
    Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
    Set-Service -Name $Name -StartupType Disabled -ErrorAction SilentlyContinue
    $now = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($null -ne $now) {
        Write-Log "service $Name -> $($now.Status)/$($now.StartType)"
    } else {
        Write-Log "service $Name -> disabled (state unavailable)" 'WARN'
    }
}

function Disable-TaskSafe {
    param([string]$Path, [string]$Name)
    $task = Get-ScheduledTask -TaskPath $Path -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Log "task not found: $Path$Name" 'WARN'
        return
    }
    Disable-ScheduledTask -TaskPath $Path -TaskName $Name -ErrorAction SilentlyContinue | Out-Null
    $now = Get-ScheduledTask -TaskPath $Path -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -ne $now) {
        Write-Log "task $Path$Name -> $($now.State)"
    }
}

function Invoke-PowerCfg {
    param([string[]]$PowerArgs)
    & powercfg.exe @PowerArgs 2>&1 | ForEach-Object { Write-Log "powercfg: $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "powercfg failed: $($PowerArgs -join ' ')" 'WARN'
        return $false
    }
    return $true
}

function Get-FeatureState {
    $state = [ordered]@{}
    foreach ($name in @('QEMU-GA', 'vgpusrv', 'VioGpuSrv', 'VioGpuDod', 'BalloonService', 'WinFsp', 'viofs', 'VirtioFsSvc', 'vioserial', 'sshd')) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($null -ne $service) {
            $state[$name] = [string]$service.Status
        } else {
            $state[$name] = 'absent'
        }
    }
    try {
        $display = @(Get-PnpDevice -Class Display -ErrorAction SilentlyContinue |
            Where-Object { $_.FriendlyName -match 'vgpu|VirtIO' } |
            ForEach-Object { "$($_.FriendlyName):$($_.Status)" })
        $state['Display'] = ($display -join '; ')
    } catch {
        $state['Display'] = 'query-failed'
    }
    try {
        $state['USB_OK'] = [string](@(Get-PnpDevice -Class USB -ErrorAction SilentlyContinue |
            Where-Object { $_.Status -eq 'OK' }).Count)
    } catch {
        $state['USB_OK'] = 'query-failed'
    }
    return $state
}

function Set-HiveValues {
    param(
        [string]$MountKey,
        [string]$HivePath,
        $Values
    )
    $resolved = [Environment]::ExpandEnvironmentVariables($HivePath)
    if (-not (Test-Path -Path $resolved)) {
        Write-Log "user hive not found: $resolved" 'WARN'
        return
    }
    & reg.exe unload $MountKey 2>&1 | Out-Null
    & reg.exe load $MountKey $resolved 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "cannot load user hive (in use?): $resolved" 'WARN'
        return
    }
    try {
        foreach ($value in $Values) {
            Set-RegValue -Key "$MountKey\$($value.key)" -Name $value.name -Type $value.type -Data $value.data
        }
        Write-Log "user hive written: $resolved"
    } finally {
        & reg.exe unload $MountKey 2>&1 | Out-Null
    }
}

$before = Get-FeatureState

foreach ($name in $services) {
    Disable-ServiceSafe -Name $name
}
foreach ($task in $tasks) {
    Disable-TaskSafe -Path $task.path -Name $task.name
}
foreach ($value in $hklm) {
    Set-RegValue -Key $value.key -Name $value.name -Type $value.type -Data $value.data
}
foreach ($command in $commands) {
    $commandArgs = @($command.args)
    & $command.file @commandArgs 2>&1 | ForEach-Object { Write-Log "command: $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "command failed: $($command.file) $($command.args -join ' ')" 'WARN'
    }
}

if (-not $SkipUserSettings) {
    if ($identity.Name -notmatch 'SYSTEM$') {
        foreach ($value in $hkcu) {
            Set-RegValue -Key "HKCU\$($value.key)" -Name $value.name -Type $value.type -Data $value.data
        }
    } else {
        Write-Log 'SYSTEM context: HKCU skipped; Default hive will be used' 'WARN'
    }
    $defaultHive = Join-Path (Split-Path -Parent $env:SystemRoot) 'Users\Default\NTUSER.DAT'
    Set-HiveValues -MountKey 'HKU\VemuDefault' -HivePath $defaultHive -Values $hkcu
    if ($UserHive) {
        Set-HiveValues -MountKey 'HKU\VemuUser' -HivePath $UserHive -Values $hkcu
    }
}

if (-not $SkipPower) {
    $active = Invoke-PowerCfg -PowerArgs @('/setactive', $power.activeScheme)
    if (-not $active) {
        Write-Log 'High performance scheme not found; trying SCHEME_MIN' 'WARN'
        Invoke-PowerCfg -PowerArgs @('/setactive', 'SCHEME_MIN') | Out-Null
    }
    foreach ($pair in $power.change) {
        Invoke-PowerCfg -PowerArgs @('/change', "-$($pair[0])", "$($pair[1])") | Out-Null
    }
    if ($power.hibernateOff) {
        Invoke-PowerCfg -PowerArgs @('/hibernate', 'off') | Out-Null
    }
    if ($power.hiberbootOff) {
        Set-RegValue -Key 'HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name 'HiberbootEnabled' -Type 'REG_DWORD' -Data '0'
    }
    if ($power.usbSelectiveSuspendOff) {
        Invoke-PowerCfg -PowerArgs @('/setacvalueindex', 'SCHEME_CURRENT', '2a737441-1930-4402-8d77-b2bebba308a3', '48e6b7a6-50f5-4782-a5d4-53bb8f07e226', '0') | Out-Null
        Invoke-PowerCfg -PowerArgs @('/setdcvalueindex', 'SCHEME_CURRENT', '2a737441-1930-4402-8d77-b2bebba308a3', '48e6b7a6-50f5-4782-a5d4-53bb8f07e226', '0') | Out-Null
    }
    if ($power.pcieAspmOff) {
        Invoke-PowerCfg -PowerArgs @('/setacvalueindex', 'SCHEME_CURRENT', '501a4d13-42af-4429-9fd1-a8218c268e20', 'ee12f906-d277-404b-b6da-e5fa1a576df5', '0') | Out-Null
        Invoke-PowerCfg -PowerArgs @('/setdcvalueindex', 'SCHEME_CURRENT', '501a4d13-42af-4429-9fd1-a8218c268e20', 'ee12f906-d277-404b-b6da-e5fa1a576df5', '0') | Out-Null
    }
    if ($power.setActiveAfter) {
        Invoke-PowerCfg -PowerArgs @('/setactive', 'SCHEME_CURRENT') | Out-Null
    }
    Invoke-PowerCfg -PowerArgs @('/getactivescheme') | Out-Null
}

$after = Get-FeatureState
$failures = @()
foreach ($key in $before.Keys) {
    if (@('Display', 'USB_OK') -contains $key) { continue }
    if ($before[$key] -eq 'Running' -and $after[$key] -ne 'Running') {
        $failures += "$key was Running, now $($after[$key])"
    }
}
if ($before['Display'] -match 'OK' -and $after['Display'] -notmatch 'OK') {
    $failures += "display device lost OK: $($after['Display'])"
}
if ($before['USB_OK'] -match '^\d+$' -and $after['USB_OK'] -match '^\d+$' -and [int]$after['USB_OK'] -lt [int]$before['USB_OK']) {
    Write-Log "USB device count dropped: $($before['USB_OK']) -> $($after['USB_OK'])" 'WARN'
}

Write-Log 'protected feature state:'
foreach ($key in $after.Keys) {
    Write-Log "  $key = $($after[$key])"
}
if ($failures.Count -gt 0) {
    foreach ($failure in $failures) {
        Write-Log "PROTECTED FEATURE REGRESSION: $failure" 'ERROR'
    }
    Save-Log
    exit 1
}

Write-Log "done: profile=$Profile. Reboot to apply service and shell changes."
Save-Log
exit 0
