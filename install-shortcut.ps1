$ErrorActionPreference = 'Stop'
# Derive the Unicode directory inside PowerShell, avoiding ANSI argv decoding.
$WorkingDirectory = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop) { exit 1 }
[System.IO.Directory]::CreateDirectory($desktop) | Out-Null
$shortcutPath = Join-Path $desktop "Breeze Live Room.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = (Join-Path $WorkingDirectory "start.bat")
$shortcut.WorkingDirectory = $WorkingDirectory
$shortcut.WindowStyle = 1
$shortcut.Description = "Breeze Live Room"
$shortcut.Save()
$saved = $shell.CreateShortcut($shortcutPath)
if ($saved.TargetPath -ne (Join-Path $WorkingDirectory "start.bat")) {
    throw 'The desktop shortcut target was not preserved.'
}
