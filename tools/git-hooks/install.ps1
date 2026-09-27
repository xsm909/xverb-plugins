# Puts this repository's hooks where git runs them, on Windows. Git for
# Windows runs hooks with its own sh, so the same scripts serve; they only
# have to be copied. Run once per clone:
#
#   powershell -ExecutionPolicy Bypass -File tools\git-hooks\install.ps1
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# Asked from the top of the working tree, so a relative answer is relative to
# something known; --git-path answers relative to where git was asked.
$top = git -C $here rev-parse --show-toplevel
$hooks = git -C $top rev-parse --git-path hooks
if (-not [System.IO.Path]::IsPathRooted($hooks)) {
  $hooks = Join-Path $top $hooks
}
New-Item -ItemType Directory -Force -Path $hooks | Out-Null
foreach ($hook in 'commit-msg', 'pre-push') {
  $source = Join-Path $here $hook
  if (Test-Path $source) {
    Copy-Item $source (Join-Path $hooks $hook) -Force
    Write-Output "installed $(Join-Path $hooks $hook)"
  }
}
