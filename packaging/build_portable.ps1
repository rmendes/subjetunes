# packaging/build_portable.ps1 : build the portable Subjetunes folder and zip.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1            # full (with CLAP: torch CPU + transformers)
#   powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -Lite      # no torch, ~300 MB smaller
#   ... -WithFFmpeg   also bundles ffmpeg (MP3/M4A decoding fallback)
#   ... -Quick        reuse the runtime when Python + requirements are unchanged; only sync the app
#                     (what scripts/dev.py runs on every change: seconds instead of minutes). No zip.
#   ... -NoZip        skip the zip
#
# Your data\ folder and config\user.yaml inside dist\Subjetunes are never deleted by a rebuild.
#
# Output: dist\Subjetunes\ (run Subjetunes.exe) and dist\Subjetunes-<version>-win64.zip.
# Needs internet while building. The result needs no Python, no admin rights and no installer,
# and writes only inside its own folder (data\). Windows 11 already has the WebView2 runtime.

param(
    [switch]$Lite,
    [switch]$WithFFmpeg,
    [switch]$Quick,
    [switch]$NoZip,
    [string]$PythonVersion = "3.12.10",
    [string]$Version = "0.9.1"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x faster without the progress bar
$repo = Split-Path $PSScriptRoot -Parent
$dist = Join-Path $repo "dist"
$out = Join-Path $dist "Subjetunes"
$cache = Join-Path $dist ".cache"
$runtime = Join-Path $out "runtime"
New-Item -ItemType Directory -Force $cache | Out-Null
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# One build at a time (dev.py may ask for another while one runs; it queues instead).
$lock = Join-Path $dist ".build.lock"
if ((Test-Path $lock) -and ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalMinutes -lt 60) {
    throw "Another build is running ($lock). Delete that file if it isn't."
}
New-Item -ItemType File -Force $lock | Out-Null
try {

function Fetch($url, $file) {
    $dest = Join-Path $cache $file
    if (-not (Test-Path $dest)) { Write-Host "Downloading $url"; Invoke-WebRequest $url -OutFile $dest }
    return $dest
}

# The build packs whatever subjetunes\ is next to packaging\. Run it from your full repo
# (C:\subjetunes), not from a folder holding only the update zip, or the analysis engine is missing.
if (-not (Test-Path (Join-Path $repo "subjetunes\cli.py"))) {
    Write-Warning "subjetunes\cli.py not found in $repo. The app will build, but without the analysis engine.`nUnzip the update over your full repo (C:\subjetunes) and run the build from there."
}

# What the runtime was built from. Same stamp = the runtime can be reused as it is.
$reqs = @("requirements-app.txt", "requirements-ml.txt") | ForEach-Object { Join-Path $repo $_ } | Where-Object { Test-Path $_ }
$stamp = "python=$PythonVersion lite=$Lite " + (($reqs | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash.Substring(0, 16) }) -join " ")
$stampFile = Join-Path $runtime ".deps-stamp"
$py = Join-Path $runtime "python.exe"
# A runtime from before stamps existed is adopted once instead of rebuilt (it came from these files).
if ($Quick -and (Test-Path $py) -and -not (Test-Path $stampFile)) {
    Set-Content $stampFile $stamp -Encoding ascii
    Write-Host "== Adopted the existing runtime (rebuild without -Quick if it misbehaves)"
}
$reuse = $Quick -and (Test-Path $py) -and (Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $stamp)

New-Item -ItemType Directory -Force $out | Out-Null
if ($reuse) {
    Write-Host "== Runtime unchanged, reused"
} else {
    if ($Quick) { Write-Host "== Runtime missing or requirements changed: building it (first time takes a while)" }
    Write-Host "== Embedded Python $PythonVersion"
    if (Test-Path $runtime) { Remove-Item $runtime -Recurse -Force }      # only the runtime; data\ stays
    New-Item -ItemType Directory -Force $runtime | Out-Null
    $zip = Fetch "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" "python-$PythonVersion-embed-amd64.zip"
    Expand-Archive $zip $runtime
    $tag = ($PythonVersion -split "\.")[0..1] -join ""
    # The ._pth file fixes sys.path for the embedded runtime (PYTHONPATH is ignored when it exists).
    @(
        "python$tag.zip",
        ".",
        "Lib\site-packages",
        "..\app",
        "import site"
    ) | Set-Content (Join-Path $runtime "python$tag._pth") -Encoding ascii

    Write-Host "== pip"
    $getpip = Fetch "https://bootstrap.pypa.io/get-pip.py" "get-pip.py"
    & $py $getpip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip failed" }

    Write-Host "== Dependencies"
    & $py -m pip install --no-warn-script-location -r (Join-Path $repo "requirements-app.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    if (-not $Lite) {
        & $py -m pip install --no-warn-script-location torch --index-url https://download.pytorch.org/whl/cpu
        if ($LASTEXITCODE -ne 0) { throw "torch install failed" }
        $ml = Join-Path $repo "requirements-ml.txt"
        if (Test-Path $ml) { & $py -m pip install --no-warn-script-location -r $ml }
        else { & $py -m pip install --no-warn-script-location "transformers>=4.40" }
        if ($LASTEXITCODE -ne 0) { throw "transformers install failed" }
    }

    Write-Host "== Trim"
    Get-ChildItem $runtime -Recurse -Directory -Include "__pycache__", "tests" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch "\\(numpy|scipy)\\.*testing" } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $runtime "Scripts") -Recurse -Force -ErrorAction SilentlyContinue
    Set-Content $stampFile $stamp -Encoding ascii
}

Write-Host "== App"
function Mirror($from, $to, [string[]]$extra) {
    # /MIR removes files you deleted from the repo; exit codes 0-7 mean success for robocopy.
    robocopy $from $to /MIR /XD __pycache__ data /XF *.pyc @extra /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying $from failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}
$app = Join-Path $out "app"
Mirror (Join-Path $repo "subjetunes") (Join-Path $app "subjetunes") @()
Mirror (Join-Path $repo "config") (Join-Path $out "config") @("user.yaml")
Copy-Item (Join-Path $PSScriptRoot "Subjetunes.exe") $out -Force
foreach ($f in "README.md", "TAGGING.md", "DESKTOP.md", "HANDOVER.md") {
    $p = Join-Path $repo $f
    if (Test-Path $p) { Copy-Item $p $out -Force }
}
New-Item -ItemType Directory -Force (Join-Path $out "data"), (Join-Path $out "tools") | Out-Null
# Earlier builds let the engine keep its cache in app\data; it lives in data\ now.
$old = Join-Path $app "data"
if (Test-Path (Join-Path $old "cache.sqlite")) {
    if (-not (Test-Path (Join-Path $out "data\cache.sqlite"))) {
        Get-ChildItem $old -Filter "cache.sqlite*" | Move-Item -Destination (Join-Path $out "data")
        Write-Host "   moved the analysis cache from app\data to data\"
    }
}
& $py -m compileall -q (Join-Path $app "subjetunes") | Out-Null

if ($WithFFmpeg) {
    Write-Host "== ffmpeg"
    $fz = Fetch "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "ffmpeg-essentials.zip"
    $tmp = Join-Path $cache "ffmpeg"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive $fz $tmp
    $bin = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    New-Item -ItemType Directory -Force (Join-Path $out "tools\ffmpeg") | Out-Null
    Copy-Item (Join-Path $bin.DirectoryName "*") (Join-Path $out "tools\ffmpeg")
}

Write-Host "== Smoke test"
& $py -c "import subjetunes.desktop.server, subjetunes.tagging, webview, numpy, yaml, pyrekordbox, soundfile, mutagen, requests; print('imports OK')"
if ($LASTEXITCODE -ne 0) { throw "smoke test failed: the runtime is missing something" }

if (-not ($Quick -or $NoZip)) {
    Write-Host "== Zip"
    $flavour = if ($Lite) { "lite-" } else { "" }
    $z = Join-Path $dist ("Subjetunes-{0}-{1}win64.zip" -f $Version, $flavour)
    if (Test-Path $z) { Remove-Item $z }
    # tar.exe ships with Windows 10/11 and is much faster than Compress-Archive on a 1 GB folder.
    # data\ is yours, so it stays out of the zip.
    & tar.exe -a -c -f $z -C $dist --exclude "Subjetunes/data" Subjetunes
    if ($LASTEXITCODE -ne 0) { throw "zip failed" }
    Write-Host "Zip: $z"
}
Write-Host ("Built {0} in {1:n1} s. Run Subjetunes.exe." -f $out, $sw.Elapsed.TotalSeconds)
} finally {
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
