# scan-ocr のデスクトップショートカット(アイコン付き)を作成する。
# デスクトップにショートカット作成.bat から呼び出される。

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $here "run.bat"
$icon = Join-Path $here "icon.ico"
$link = Join-Path ([Environment]::GetFolderPath("Desktop")) "scan-ocr.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $here
$shortcut.IconLocation = $icon
$shortcut.Description = "scan-ocr を起動する"
$shortcut.Save()

Write-Host "デスクトップに「scan-ocr」のショートカットを作成しました。"
