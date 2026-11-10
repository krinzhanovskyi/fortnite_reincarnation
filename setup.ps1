$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$candidates = @(
    "$env:LOCALAPPDATA\Python\bin\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    'python.exe',
    'py.exe'
)
$selectedPython = $null
foreach ($candidate in $candidates) {
    try {
        $versionCheck = & $candidate -c 'import sys; print(int(sys.version_info >= (3, 11)))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $versionCheck -eq '1') {
            $selectedPython = $candidate
            break
        }
    } catch { }
}
if (-not $selectedPython) { throw 'Install Python 3.11 or newer with Tcl/Tk support, then run setup.ps1 again.' }
& $selectedPython -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv' }
& .\.venv\Scripts\python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& .\.venv\Scripts\python.exe -m reincarnation --init
if ($LASTEXITCODE -ne 0) { throw 'Configuration initialization failed' }
Write-Host 'Ready. Run start.bat. Default mode: observation only.'
