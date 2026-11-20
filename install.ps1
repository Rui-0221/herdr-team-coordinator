[CmdletBinding()]
param(
    [string] $AppHome = $(if ($env:HERDR_APP_HOME) { $env:HERDR_APP_HOME } else { Join-Path $env:USERPROFILE '.config/herdr-app' }),
    [string] $Python,
    [switch] $CheckOnly
)

$ErrorActionPreference = 'Stop'
$modName = 'herdr-team-coordinator'
$source = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$herdr = (Get-Command herdr-app.exe -CommandType Application).Source

function Find-Python([string] $Preferred) {
    $launchers = if ($Preferred) {
        @(@{ Program = $Preferred; Prefix = @() })
    } else {
        @(
            @{ Program = 'py.exe'; Prefix = @('-3') }
            @{ Program = 'python.exe'; Prefix = @() }
            @{ Program = 'python3.exe'; Prefix = @() }
        )
    }
    foreach ($candidate in $launchers) {
        $application = Get-Command $candidate.Program -CommandType Application -ErrorAction SilentlyContinue
        if (!$application) { continue }
        $arguments = @($candidate.Prefix) + @('-B', '-X', 'utf8', '-c', 'import sys; print(sys.executable if sys.version_info >= (3, 10) else str())')
        $exe = & $application.Source @arguments
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path -LiteralPath $exe -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $exe).Path
        }
    }
    throw '未找到 Python 3.10 或更高版本。使用 -Python 指定本机解释器。'
}

function Assert-OrdinaryDirectory([string] $Path) {
    if (Test-Path -LiteralPath $Path) {
        $directory = Get-Item -Force -LiteralPath $Path
        if (!$directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "安装路径须为普通目录：$Path"
        }
    }
}

function Copy-ModFiles([string] $Destination) {
    [void] [IO.Directory]::CreateDirectory($Destination)
    Get-ChildItem -LiteralPath $source -File | Where-Object {
        $_.Extension -in '.py', '.js', '.json', '.md', '.ps1', '.cmd', '.svg', '.ico', '.png' -and
        $_.Name -notin 'python-path.json', 'install.ps1', 'jsconfig.json' -and $_.Name -notlike 'test_*'
    } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $Destination }
}

if ($CheckOnly) {
    $testCopy = Join-Path ([IO.Path]::GetTempPath()) ('HerdrTeamCoordinatorCheck-' + [Guid]::NewGuid().ToString('N'))
    try {
        Copy-ModFiles $testCopy
        & $herdr check $testCopy | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Herdr 检查未通过。' }
    } finally {
        Remove-Item -LiteralPath $testCopy -Recurse -Force -ErrorAction SilentlyContinue
    }
    return
}

$appRoot = [IO.Path]::GetFullPath($AppHome)
$mods = Join-Path $appRoot 'mods'
$target = Join-Path $mods $modName
$stagingRoot = Join-Path $appRoot 'herdr-team-coordinator-install'
$installID = [Guid]::NewGuid().ToString('N')
$staging = Join-Path $stagingRoot "stage-$installID"
$backup = Join-Path $stagingRoot "backup-$installID"
foreach ($path in @($appRoot, $mods, $target, $stagingRoot)) { Assert-OrdinaryDirectory $path }
if ([string]::Equals($source, $target, [StringComparison]::OrdinalIgnoreCase)) {
    throw '请从项目源码目录运行安装脚本。'
}

$previousPythonFile = Join-Path $target 'python-path.json'
if (!$Python -and (Test-Path -LiteralPath $previousPythonFile -PathType Leaf)) {
    $Python = (Get-Content -Raw -Encoding UTF8 -LiteralPath $previousPythonFile | ConvertFrom-Json).python
}
$pythonExe = Find-Python $Python

foreach ($filename in @('coord.py', 'main.js', 'mod.json')) {
    if (!(Test-Path -LiteralPath (Join-Path $source $filename) -PathType Leaf)) {
        throw "源码缺少 $filename。"
    }
}

[void] [IO.Directory]::CreateDirectory($mods)
Copy-ModFiles $staging
$pythonConfig = @{ python = $pythonExe } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $staging 'python-path.json'), "$pythonConfig`n", [Text.UTF8Encoding]::new($false))

& $herdr check $staging | Out-Host
if ($LASTEXITCODE -ne 0) { throw '待安装副本未通过 Herdr 检查。' }

if (Test-Path -LiteralPath $target -PathType Container) {
    $oldHelper = Join-Path $target 'coord.py'
    if (!(Test-Path -LiteralPath $oldHelper -PathType Leaf)) {
        throw "发现已有目录，无法确认属于团队协调器：$target"
    }
    $result = & $pythonExe -B -X utf8 $oldHelper local stop
    if ($LASTEXITCODE -ne 0) { throw "旧调度器停机未完成：$result" }
    Move-Item -LiteralPath $target -Destination $backup
}
try {
    Move-Item -LiteralPath $staging -Destination $target
} catch {
    if ((Test-Path -LiteralPath $backup -PathType Container) -and !(Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $backup -Destination $target
    }
    throw
}

try {
    $sandboxSid = ([System.Security.Principal.NTAccount]'CodexSandboxUsers').Translate([System.Security.Principal.SecurityIdentifier]).Value
    & icacls $target /grant "*${sandboxSid}:(OI)(CI)(RX)" | Out-Null
} catch {}

Write-Output "团队协调器已安装到 $target"
if (Test-Path -LiteralPath $backup) { Write-Output "上一版本保存在 $backup" }
Write-Output '打开方式：herdr-app do mod.open --name herdr-team-coordinator'
