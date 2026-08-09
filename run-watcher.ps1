# Starts the end-of-game watcher. Leave this window open while you play.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m lolhist watch
