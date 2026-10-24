# Form Studio installer: checks the CadQuery Python, installs the mesh/printing helpers into it,
# and creates "Form Studio" shortcuts on the Desktop and in the Start menu (no console window).
# Safe to re-run. Result: install-status.json next to this script.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$App = $PSScriptRoot
$status = [ordered]@{ started = (Get-Date).ToString('s'); steps = [ordered]@{} }
function Save { $status | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $App 'install-status.json') -Encoding UTF8 }
function Step([string]$Name, [scriptblock]$Body) {
    Write-Host "== $Name"
    try { $r = & $Body | Select-Object -Last 1; $status.steps[$Name] = if ($r) { "$r" } else { 'ok' }; Write-Host "   $($status.steps[$Name])"; Save; return $true }
    catch { $status.steps[$Name] = "FAILED: $($_.Exception.Message)"; Write-Host "   $($status.steps[$Name])"; Save; return $false }
}
function Run([string]$Exe, [string[]]$ArgList) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $Exe @ArgList 2>&1 | ForEach-Object { "$_" } | Out-Host; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "$([IO.Path]::GetFileName($Exe)) exited with $code" }
}

$candidates = @($env:PHOTO2CAD_CAD_PYTHON, [Environment]::GetEnvironmentVariable('PHOTO2CAD_CAD_PYTHON', 'User'),
    (Join-Path $env:USERPROFILE 'mcp-servers\overnight-ai\cadenv\Scripts\python.exe')) | Where-Object { $_ -and (Test-Path $_) }
$Py = $candidates | Select-Object -First 1
$PyW = if ($Py) { Join-Path (Split-Path $Py) 'pythonw.exe' } else { $null }

$ok = Step 'cad-python' {
    if (-not $Py) { throw 'CadQuery Python (overnight-ai\cadenv) not found. Run photo_tools_setup first.' }
    if (-not (Test-Path $PyW)) { throw "pythonw.exe missing next to $Py" }
    Run $Py @('-c', 'import cadquery; print("cadquery", cadquery.__version__)')
    $Py
}
if ($ok) {
    Step 'print-helpers' {
        # Used by "Make printable" (lay flat, split to the bed, dowel holes) and Mesh mode.
        Run $Py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', 'numpy', 'trimesh', 'manifold3d', 'mapbox-earcut', 'shapely', 'rtree', 'scipy', 'networkx')
        Run $Py @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--no-deps', 'opencv-python-headless==4.11.0.86')
        Run $Py @('-c', 'import trimesh, manifold3d, shapely, rtree, cv2; print("trimesh", trimesh.__version__, "opencv", cv2.__version__)')
        'installed'
    } | Out-Null
    Step 'shortcuts' {
        $shell = New-Object -ComObject WScript.Shell
        $made = @()
        foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) ''))) {
            $lnk = $shell.CreateShortcut((Join-Path $dir 'Form Studio.lnk'))
            $lnk.TargetPath = $PyW
            $lnk.Arguments = '-B "' + (Join-Path $App 'app.py') + '"'
            $lnk.WorkingDirectory = $App
            $lnk.IconLocation = (Join-Path $App 'static\FormStudio.ico') + ',0'
            $lnk.Description = 'Form Studio - photo and chat to CAD with your local AI'
            $lnk.Save()
            $made += $lnk.FullName
        }
        $made -join ' | '
    } | Out-Null
}
$status.finished = (Get-Date).ToString('s'); Save
Write-Host ''
Write-Host 'Done. Open Form Studio from the Desktop or Start menu shortcut.'
