param(
    [Parameter(Mandatory = $true)][string]$WorkingDirectory
)
# %~dp0 is passed as "<dir>\." so the quoted argument does not end in a backslash.
$WorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop) { exit 1 }
$shortcutPath = Join-Path $desktop "禪譯聽眾房.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $WorkingDirectory "start.bat"
$shortcut.WorkingDirectory = $WorkingDirectory
$shortcut.WindowStyle = 1
$shortcut.Description = "禪譯聽眾房主持端"
$shortcut.Save()
