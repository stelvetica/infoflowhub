$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = "C:\Users\TB14Plus\anaconda3\python.exe"
$command = "Set-Location '$root'; & '$python' -m web.server"

Start-Process powershell -ArgumentList "-NoProfile", "-WindowStyle", "Hidden", "-Command", $command -WindowStyle Hidden
