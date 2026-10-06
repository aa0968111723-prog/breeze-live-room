param(
    [switch]$CheckOnly,
    [string]$DesktopPath
)
$ErrorActionPreference = 'Stop'
# Use the Unicode Shell Link interface. WScript.Shell loses characters on an
# English Windows installation when a target contains a Chinese directory.

function Test-BreezeNonAsciiText([string]$Value) {
    if ([string]::IsNullOrEmpty($Value)) { return $false }
    foreach ($ch in $Value.ToCharArray()) {
        if ([int]$ch -gt 127) { return $true }
    }
    return $false
}

function Test-BreezeWritableDirectory([string]$Path) {
    try {
        $null = [System.IO.Directory]::CreateDirectory($Path)
        $probe = Join-Path $Path ([guid]::NewGuid().ToString('n') + '.tmp')
        [System.IO.File]::WriteAllText($probe, 'ok')
        try { [System.IO.File]::Delete($probe) } catch { }
        return $true
    } catch {
        return $false
    }
}

function Resolve-BreezeAsciiPath([string]$Root, [string]$Leaf) {
    if ([string]::IsNullOrEmpty($Root)) { return '' }
    if ($Root -match '^[A-Za-z]:$') { $Root = $Root + '\' }
    try {
        $candidate = [System.IO.Path]::GetFullPath((Join-Path $Root $Leaf))
    } catch {
        return ''
    }
    if (Test-BreezeNonAsciiText $candidate) { return '' }
    return $candidate
}

function Get-BreezeAsciiTemp {
    # csc.exe (Windows PowerShell 5.1) fails when TEMP is not pure ASCII.
    foreach ($candidate in @(
        (Resolve-BreezeAsciiPath $env:ProgramData 'BreezeLiveRoom\tmp'),
        (Resolve-BreezeAsciiPath $env:PUBLIC 'BreezeLiveRoom\tmp'),
        (Resolve-BreezeAsciiPath $env:SystemDrive 'BreezeLiveRoomTmp')
    )) {
        if ([string]::IsNullOrEmpty($candidate)) { continue }
        if (Test-BreezeWritableDirectory $candidate) { return $candidate }
    }
    throw 'No ASCII-only writable directory is available for shortcut compilation.'
}

$breezeOriginalTemp = $env:TEMP
$breezeOriginalTmp = $env:TMP
try {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
class BreezeShellLink { }

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IBreezeShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr data, uint flags);
    void GetIDList(out IntPtr list);
    void SetIDList(IntPtr list);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetHotkey(out short hotkey);
    void SetHotkey(short hotkey);
    void GetShowCmd(out int command);
    void SetShowCmd(int command);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public static class BreezeDesktopShortcut {
    public static void Create(string target, string directory, string link) {
        IBreezeShellLinkW shell = (IBreezeShellLinkW)new BreezeShellLink();
        try {
            shell.SetPath(target);
            shell.SetWorkingDirectory(directory);
            shell.SetDescription("Breeze Live Room");
            shell.SetShowCmd(1);
            ((IPersistFile)shell).Save(link, true);
        } finally { Marshal.FinalReleaseComObject(shell); }
    }
    public static void Verify(string target, string directory, string link) {
        IBreezeShellLinkW shell = (IBreezeShellLinkW)new BreezeShellLink();
        try {
            ((IPersistFile)shell).Load(link, 0);
            StringBuilder savedTarget = new StringBuilder(32768);
            StringBuilder savedDirectory = new StringBuilder(32768);
            shell.GetPath(savedTarget, savedTarget.Capacity, IntPtr.Zero, 4);
            shell.GetWorkingDirectory(savedDirectory, savedDirectory.Capacity);
            if (!String.Equals(savedTarget.ToString(), target, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(savedDirectory.ToString(), directory, StringComparison.OrdinalIgnoreCase)) {
                throw new InvalidOperationException("The Unicode shortcut target or directory was not preserved.");
            }
        } finally { Marshal.FinalReleaseComObject(shell); }
    }
}
'@
} finally {
    $env:TEMP = $breezeOriginalTemp
    $env:TMP = $breezeOriginalTmp
}

$WorkingDirectory = $PSScriptRoot
if ([string]::IsNullOrEmpty($DesktopPath)) {
    $desktop = [Environment]::GetFolderPath('Desktop')
} else {
    $desktop = $DesktopPath
}
if (-not $desktop) { exit 1 }
[System.IO.Directory]::CreateDirectory($desktop) | Out-Null
foreach ($entry in @(@('Breeze Live Room', 'start.bat'), @('Breeze Update', 'update.bat'), @('Breeze Doctor', 'doctor.bat'))) {
    $shortcutPath = Join-Path $desktop ($entry[0] + '.lnk')
    $target = Join-Path $WorkingDirectory $entry[1]
    if (-not $CheckOnly) {
        [BreezeDesktopShortcut]::Create($target, $WorkingDirectory, $shortcutPath)
    } elseif (-not (Test-Path -LiteralPath $shortcutPath)) {
        Write-Output ("Shortcut not found: " + $shortcutPath)
        exit 2
    }
    try {
        [BreezeDesktopShortcut]::Verify($target, $WorkingDirectory, $shortcutPath)
    } catch {
        Write-Output ('Shortcut target or directory is incorrect: ' + $shortcutPath + ' (' + $_.Exception.Message + ')')
        exit 1
    }
}
