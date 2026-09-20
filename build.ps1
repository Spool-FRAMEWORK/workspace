# Builds Spool modules in dependency order through the workspace reactor.
# Windows equivalent of build.sh.
#
#   .\build.ps1                     build and install everything
#   .\build.ps1 infrastructure      that module, what it depends on, and what depends on it
#   .\build.ps1 core -Tests         same, running the tests too
#   .\build.ps1 dsl -Fast           skip rebuilding upstream modules (they must already be in ~/.m2)
#   .\build.ps1 dsl -NoInstall      package only, leave ~/.m2 untouched
param(
    [string]$Module,
    [switch]$Tests,
    [switch]$Fast,
    [switch]$NoInstall
)

$mvn = Join-Path $PSScriptRoot 'mvnw.cmd'
if (-not (Test-Path $mvn)) { $mvn = 'mvn' }

$goal = if ($NoInstall) { 'package' } else { 'install' }
$mvnArgs = @('-f', (Join-Path $PSScriptRoot 'pom.xml'), '-T', '1C', $goal)

if (-not $Tests) { $mvnArgs += '-DskipTests' }

if ($Module) {
    $mvnArgs += @('-pl', ":$Module", '-amd')
    if (-not $Fast) { $mvnArgs += '-am' }
}

Write-Host "$mvn $($mvnArgs -join ' ')"
& $mvn @mvnArgs
exit $LASTEXITCODE
