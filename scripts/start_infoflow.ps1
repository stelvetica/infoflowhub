$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$command = "Set-Location '$root'; & '$npm' run dev"

Start-Process powershell -ArgumentList "-NoProfile", "-WindowStyle", "Hidden", "-Command", $command -WindowStyle Hidden
