<#
refresh-library.ps1 — Windows counterpart of refresh-library.zsh. Download any new/updated
catalog + ASDSO files, then re-index them so `vor` answers stay current. Safe to run
repeatedly; files already downloaded are skipped and nothing is deleted.

  .\refresh-library.ps1                      # all catalog groups + ASDSO pages, then ingest
  .\refresh-library.ps1 -CatalogArgs '--catalog','software'   # only one group
  .\refresh-library.ps1 -NoIngest            # download only
  .\refresh-library.ps1 -DryRun              # list only, fetch nothing, no ingest

Schedule it with Task Scheduler (user-level, no admin), e.g. weekly Monday 03:00:
  $a = New-ScheduledTaskAction -Execute 'powershell.exe' `
         -Argument '-ExecutionPolicy Bypass -File "<abs path>\refresh-library.ps1"'
  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3am
  Register-ScheduledTask -TaskName 'vor refresh' -Action $a -Trigger $t
Nothing leaves the machine.
#>
[CmdletBinding()]
param(
  [string[]]$CatalogArgs,
  [switch]$NoIngest,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py   = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
# Where vor.py reads from, by its own rule (see vor_home in fetch_asdso.py).
$Home2 = if ($env:VOR_HOME) { $env:VOR_HOME } else { (& $Py (Join-Path $Here 'fetch_asdso.py') --print-home) }

$pass = @()
if ($CatalogArgs) { $pass += $CatalogArgs }
if (-not ($pass -join ' ').Contains('--catalog')) { $pass = @('--catalog','--asdso') + $pass }
if ($DryRun) { $pass += '--dry-run'; $NoIngest = $true }

Write-Host "== vor library refresh =="
Write-Host "Library root: $Home2  ($([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mmZ')))"

New-Item -ItemType Directory -Force -Path (Join-Path $Home2 'library'), (Join-Path $Home2 'reports') | Out-Null
& $Py (Join-Path $Here 'fetch_asdso.py') --urls (Join-Path $Home2 'library\library-urls.txt')
& $Py (Join-Path $Here 'fetch_asdso.py') @pass

if (-not $NoIngest) {
  Write-Host 'Indexing new files (vor ingest)...'
  $cmd = Join-Path $env:USERPROFILE 'local-ai\vor\vor.cmd'   # written by the installer
  if (Test-Path $cmd) { & $cmd ingest }
  else { & $Py (Join-Path $Here 'vor.py') ingest }
}
Write-Host "Done. Library in $Home2\library."
