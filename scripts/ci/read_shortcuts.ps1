param(
    [Parameter(Mandatory = $true)]
    [string]$Out,
    [string]$InstallDir,
    [string]$DesktopPath,
    [switch]$NoVerify
)
$ErrorActionPreference = 'Stop'
# Read the three Breeze shortcuts with WScript.Shell and write a UTF-8 (no BOM)
# JSON array to -Out. Unless -NoVerify, exit 1 when a link is missing, its target
# is not InstallDir joined with the bat name, its working directory is not
# InstallDir, or its arguments are non-empty. Messages name the link and are ASCII.

if ([string]::IsNullOrEmpty($InstallDir)) {
    # Two levels above this file: the install root when invoked as .\scripts\ci\read_shortcuts.ps1.
    $InstallDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
if ([string]::IsNullOrEmpty($DesktopPath)) {
    $DesktopPath = [Environment]::GetFolderPath('Desktop')
}

$entries = @(
    @{ Name = 'Breeze Live Room'; Bat = 'start.bat' },
    @{ Name = 'Breeze Update'; Bat = 'update.bat' },
    @{ Name = 'Breeze Doctor'; Bat = 'doctor.bat' }
)

$shell = New-Object -ComObject WScript.Shell
$rows = @()
$failures = New-Object 'System.Collections.Generic.List[string]'
foreach ($entry in $entries) {
    $linkName = [string]$entry.Name
    $batName = [string]$entry.Bat
    $shortcutPath = Join-Path $DesktopPath ($linkName + '.lnk')
    $exists = Test-Path -LiteralPath $shortcutPath
    $target = ''
    $workdir = ''
    $linkArgs = ''
    $icon = ''
    if ($exists) {
        $link = $shell.CreateShortcut($shortcutPath)
        $target = [string]$link.TargetPath
        $workdir = [string]$link.WorkingDirectory
        $linkArgs = [string]$link.Arguments
        $icon = [string]$link.IconLocation
    }
    $rows += [pscustomobject]@{
        path    = $shortcutPath
        exists  = [bool]$exists
        target  = $target
        workdir = $workdir
        args    = $linkArgs
        icon    = $icon
    }
    if ($NoVerify) { continue }
    if (-not $exists) {
        $null = $failures.Add('Shortcut not found: ' + $linkName)
        continue
    }
    $expectedTarget = Join-Path $InstallDir $batName
    if (-not [string]::Equals($target, $expectedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
        $null = $failures.Add('Shortcut target mismatch: ' + $linkName)
    }
    if (-not [string]::Equals($workdir, $InstallDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        $null = $failures.Add('Shortcut directory mismatch: ' + $linkName)
    }
    if (-not [string]::IsNullOrEmpty($linkArgs)) {
        $null = $failures.Add('Shortcut arguments are not empty: ' + $linkName)
    }
}

$parent = [System.IO.Path]::GetDirectoryName($Out)
if (-not [string]::IsNullOrEmpty($parent)) {
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
}
$json = ConvertTo-Json -InputObject @($rows) -Depth 4
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($Out, $json, $utf8)

if ($failures.Count -gt 0) {
    foreach ($message in $failures) {
        Write-Output $message
    }
    exit 1
}
exit 0
