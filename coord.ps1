$ErrorActionPreference = 'Stop'
$helper = Join-Path $PSScriptRoot 'coord.py'
$saved = Join-Path $PSScriptRoot 'python-path.json'
if (Test-Path -LiteralPath $saved -PathType Leaf) {
    $python = (Get-Content -Encoding UTF8 -Raw -LiteralPath $saved | ConvertFrom-Json).python
    & $python -B -X utf8 $helper @args
} elseif ($launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue) {
    & $launcher.Source -3 -B -X utf8 $helper @args
} else {
    & python.exe -B -X utf8 $helper @args
}
exit $LASTEXITCODE
