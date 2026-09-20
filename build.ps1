# Builds Spool modules in dependency order through the workspace reactor.
# Windows equivalent of build.sh.
#
#   .\build.ps1                     build and install everything
#   .\build.ps1 infrastructure      that module, what depends on it, and whatever is needed to build them
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
    if ($Fast) {
        $mvnArgs += @('-pl', ":$Module", '-amd')
    } else {
        # The dependents of a module can need other modules too (infrastructure needs janitor,
        # mounter and ingester), so first list the module plus its dependents, then build that
        # set together with everything upstream of it.
        $listing = & $mvn -B -f (Join-Path $PSScriptRoot 'pom.xml') -pl ":$Module" -amd validate 2>&1 |
            ForEach-Object { $_.ToString() }
        if ($LASTEXITCODE -ne 0) { $listing | Write-Host; exit $LASTEXITCODE }
        $names = $listing |
            Select-String -Pattern '^\[INFO\] Building \S+:(\S+) \S+(\s+\[\d+/\d+\])?\s*$' |
            ForEach-Object { ':' + $_.Matches[0].Groups[1].Value }
        if (-not $names) {
            Write-Error "Could not work out which modules to build for '$Module'"
            exit 1
        }
        $mvnArgs += @('-pl', ($names -join ','), '-am')
    }
}

Write-Host "$mvn $($mvnArgs -join ' ')"
& $mvn @mvnArgs
exit $LASTEXITCODE
