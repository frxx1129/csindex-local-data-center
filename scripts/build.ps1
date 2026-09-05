Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Decode-Utf8 {
    param([Parameter(Mandatory = $true)][string]$Base64)
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Base64))
}

function Assert-NotLocalCDrive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($root -and $root.ToUpperInvariant().StartsWith('C:\')) {
        throw "$Label is on local C drive: $full. Move the project to E/D/F or another non-C drive first."
    }
    return $full
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $childFull = [System.IO.Path]::GetFullPath($Child).TrimEnd('\')
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    if ($childFull -eq $parentFull -or -not $childFull.StartsWith($parentFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside the allowed directory: $childFull"
    }
    return $childFull
}

function Assert-AsciiPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($Path.ToCharArray() | Where-Object { [int][char]$_ -gt 127 }) {
        throw "$Label must be ASCII for Nuitka/MSVC: $Path"
    }
    return $Path
}

function Remove-VerifiedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AllowedParent,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $target = Assert-ChildPath -Child $Path -Parent $AllowedParent -Label $Label
    if ((Test-Path -LiteralPath $target) -and (Get-Item -LiteralPath $target).PSIsContainer) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
$ProjectPath = Assert-NotLocalCDrive -Path ([System.IO.Path]::GetFullPath($ProjectRoot)) -Label "Project root"
$ProjectParent = Assert-NotLocalCDrive -Path (Split-Path -Parent $ProjectPath) -Label "Project parent"
$WorkspaceName = "csindex-local-nuitka-work"
$AsciiWorkspace = Assert-NotLocalCDrive -Path (Join-Path $ProjectParent $WorkspaceName) -Label "ASCII build workspace"
Assert-AsciiPath -Path $AsciiWorkspace -Label "ASCII build workspace" | Out-Null
if ((Split-Path -Leaf $AsciiWorkspace) -ne $WorkspaceName) {
    throw "Refusing to clean unexpected workspace: $AsciiWorkspace"
}

$BuildRoot = Join-Path $AsciiWorkspace "build"
$TempRoot = Join-Path $BuildRoot "temp"
$CacheRoot = Join-Path $BuildRoot "cache"
$NuitkaRoot = Join-Path $BuildRoot "nuitka"
$SourceCopyRoot = Join-Path $AsciiWorkspace "source"
$DistRoot = Join-Path $ProjectPath "dist"
$ReleaseRoot = Join-Path $ProjectPath "release"
$AppName = Decode-Utf8 "5Lit6K+B5oyH5pWw5pys5Zyw5pWw5o2u5Lit5b+D"
$ReleaseAppRoot = Join-Path $ReleaseRoot $AppName
$ExeName = Decode-Utf8 "5Lit6K+B5oyH5pWw5pys5Zyw5pWw5o2u5Lit5b+DLmV4ZQ=="
$ExePath = Join-Path $DistRoot $ExeName

Set-Location $ProjectPath
Remove-VerifiedDirectory -Path $AsciiWorkspace -AllowedParent $ProjectParent -Label "ASCII build workspace"

$null = New-Item -ItemType Directory -Force $TempRoot, $CacheRoot, $NuitkaRoot, $SourceCopyRoot, $DistRoot, $ReleaseAppRoot
Copy-Item -LiteralPath (Join-Path $ProjectPath "src") -Destination $SourceCopyRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ProjectPath "README.md") -Destination $SourceCopyRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectPath "config.example.json") -Destination $SourceCopyRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectPath "pyproject.toml") -Destination $SourceCopyRoot -Force

$env:TEMP = $TempRoot
$env:TMP = $TempRoot
$env:PYTHONPYCACHEPREFIX = Join-Path $CacheRoot "pycache"
$env:NUITKA_CACHE_DIR = Join-Path $CacheRoot "nuitka-cache"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$PureLib = (& py -3.12 -S -c "import sysconfig; print(sysconfig.get_path('purelib'))").Trim()
if (-not $PureLib) {
    throw "Could not locate Python 3.12 purelib."
}

$ExternalDeps = Join-Path $ProjectParent "python-deps\csindex-local"
$SourceRoot = Join-Path $SourceCopyRoot "src"
$env:PYTHONPATH = @($PureLib, $ExternalDeps, $SourceRoot) -join [System.IO.Path]::PathSeparator

Set-Location $SourceCopyRoot
Write-Host "Project: $ProjectPath"
Write-Host "ASCII build workspace: $AsciiWorkspace"
Write-Host "TEMP/TMP: $TempRoot"
Write-Host "PYTHONPATH: $env:PYTHONPATH"
& py -3.12 -S -m nuitka --version
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka version check failed."
}

& py -3.12 -S -m nuitka `
    --standalone `
    --onefile `
    --assume-yes-for-downloads `
    --python-flag=-m `
    --windows-console-mode=disable `
    --enable-plugin=tk-inter `
    --include-package=openpyxl `
    --nofollow-import-to=PIL,numpy,lxml `
    --output-dir="$NuitkaRoot" `
    --output-filename="$ExeName" `
    src\csindex_local
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka build failed."
}

$BuiltExe = Join-Path $NuitkaRoot $ExeName
if (-not (Test-Path -LiteralPath $BuiltExe)) {
    throw "Build output not found: $BuiltExe"
}

Copy-Item -LiteralPath $BuiltExe -Destination $ExePath -Force
Copy-Item -LiteralPath $ExePath -Destination (Join-Path $ReleaseAppRoot $ExeName) -Force
Copy-Item -LiteralPath (Join-Path $ProjectPath "README.md") -Destination $ReleaseAppRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectPath "config.example.json") -Destination $ReleaseAppRoot -Force

$Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath
$Size = (Get-Item -LiteralPath $ExePath).Length
Write-Host "Built: $ExePath"
Write-Host "Size: $Size bytes"
Write-Host "SHA256: $($Hash.Hash)"
Write-Host "Release: $ReleaseAppRoot"
