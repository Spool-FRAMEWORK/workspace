# Clones the Spool repositories as siblings of this workspace folder.
# Windows equivalent of bootstrap.sh. The module list is read from pom.xml.
# Existing clones are left untouched, so it is safe to run again.
#
#   .\bootstrap.ps1                 clone every module on its develop branch
#   .\bootstrap.ps1 -WithDevenv     also clone the observability stack
param(
    [string]$Branch = 'develop',
    [switch]$WithDevenv
)

$org    = 'https://github.com/Spool-FRAMEWORK'
$parent = Split-Path $PSScriptRoot -Parent

$modules = Select-String -Path (Join-Path $PSScriptRoot 'pom.xml') -Pattern '<module>\.\./([^<]+)</module>' |
    ForEach-Object { $_.Matches[0].Groups[1].Value }

foreach ($name in $modules) {
    $target = Join-Path $parent $name
    if (Test-Path (Join-Path $target '.git')) {
        Write-Host ("{0,-16} already cloned" -f $name)
        continue
    }
    Write-Host ("{0,-16} cloning ({1})" -f $name, $Branch)
    git clone --branch $Branch "$org/$name.git" $target
    if ($LASTEXITCODE -ne 0) { Write-Error "Could not clone $name"; exit $LASTEXITCODE }
}

if ($WithDevenv) {
    $target = Join-Path $parent 'devenv'
    if (Test-Path (Join-Path $target '.git')) {
        Write-Host ("{0,-16} already cloned" -f 'devenv')
    } else {
        Write-Host ("{0,-16} cloning" -f 'devenv')
        git clone "$org/devenv.git" $target
        if ($LASTEXITCODE -ne 0) { Write-Error "Could not clone devenv"; exit $LASTEXITCODE }
    }
}

Write-Host "`nDone. Build everything with: .\build.ps1"
