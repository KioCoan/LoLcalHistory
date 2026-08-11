## Install

Download **LoLcal-History-{{VERSION}}-Setup.exe** below and run it.

It installs for the current user only — no administrator prompt — into
`%LOCALAPPDATA%\Programs\LoLcal History`, and adds a Start Menu entry. Already have it?
Run the installer over the top: it closes the running copy, upgrades in place and
reopens. Your match history is untouched.

> Windows SmartScreen will warn that the publisher is unknown, because the installer is
> not code-signed. Choose **More info → Run anyway**.

Prefer not to install? **LoLcal-History-{{VERSION}}-portable.exe** is the same app as a
single file — but the in-app updater only works with the installed version.

Verify your download against `SHA256SUMS.txt` if you like:

```powershell
Get-FileHash .\LoLcal-History-{{VERSION}}-Setup.exe -Algorithm SHA256
```

## What it does

Keeps a local record of your matches — including **ARAM: Mayhem** and **League
Classic**, which the public Riot API does not serve — by reading the League client on
your own machine. Champion, item and augment names and art all come from the client's
own files.

Nothing about your games is uploaded. The only request that leaves your machine is the
update check against this releases page, which sends no identifier and no data. Set
`LOLHIST_NO_UPDATE_CHECK=1` to switch it off.
