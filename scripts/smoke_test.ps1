param(
    [ValidateSet(1, 20, 100)]
    [int]$Count = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Decode-Utf8 {
    param([Parameter(Mandatory = $true)][string]$Base64)
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Base64))
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
$ProjectPath = [System.IO.Path]::GetFullPath($ProjectRoot)
$RootDrive = [System.IO.Path]::GetPathRoot($ProjectPath)
if ($RootDrive -and $RootDrive.ToUpperInvariant().StartsWith('C:\')) {
    $messageTemplate = Decode-Utf8 "56iL5bqP5qC555uu5b2V5L2N5LqOIEMg55uY77yaezB944CC6K+356e75Yqo5YiwIEUvRC9GIOetiemdniBDIOebmOWQjuWGjeaJp+ihjOecn+WunuaOpeWPo+a1i+ivleOAgg=="
    throw ($messageTemplate -f $ProjectPath)
}

$warningTemplate = Decode-Utf8 "5pys6ISa5pys5Lya6K6/6Zeu55yf5a6e5Lit6K+B5oyH5pWw5a6Y572R77yM5bm25oyJ5q2j5byP6ZmQ6YCf5oqT5Y+WIHswfSDkuKrmjIfmlbDjgILlj6rmiafooYzlvZPliY3moaPkvY3vvIzkuI3kvJroh6rliqjljYfnuqfliLDkuIvkuIDmoaPjgII="
Write-Warning ($warningTemplate -f $Count)
$prompt = Decode-Utf8 "56Gu6K6k57un57ut6K+36L6T5YWlIFlFUw=="
$answer = Read-Host $prompt
if ($answer -ne "YES") {
    Write-Host "Cancelled."
    exit 1
}

$env:PYTHONPATH = (Join-Path $ProjectPath "src")

& py -3.12 -m csindex_local.cli --root "$ProjectPath" init
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& py -3.12 -m csindex_local.cli --root "$ProjectPath" crawl --scope "$Count" --mode force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& py -3.12 -m csindex_local.cli --root "$ProjectPath" export --scope "fixed:$Count" --sort one_year --limit all
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
