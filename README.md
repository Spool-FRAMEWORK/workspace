# spool-framework/workspace

Development workspace for the [Spool](https://github.com/Spool-FRAMEWORK) framework.

Spool is split into one repository per module. This repository is **not** a module and is never published: it only lets you
clone all of them together, build them in dependency order, and open them as a single project in your IDE.
Each module keeps its own repository, history, version and release pipeline.

## Layout

The scripts clone the modules as siblings of this folder, so start from an empty directory:

```
spool/                  any name you like
├── workspace/          this repository
├── core/
├── validator/
├── crawler/
├── janitor/
├── mounter/
├── ingester/
├── infrastructure/
├── dsl/
├── runtime/
└── watchdog/
```

## Prerequisites

- Git
- JDK 21 or newer. Every module compiles with `--release 21` and the CI uses 21.
- Nothing else: Maven is downloaded by the included Maven Wrapper (`mvnw`).

## Quick start

macOS, Linux, or Git Bash / WSL on Windows:

```bash
mkdir spool && cd spool
git clone https://github.com/Spool-FRAMEWORK/workspace.git
cd workspace
./bootstrap.sh
./build.sh
```

Windows PowerShell:

```powershell
mkdir spool; cd spool
git clone https://github.com/Spool-FRAMEWORK/workspace.git
cd workspace
.\bootstrap.ps1
.\build.ps1
```

`bootstrap` clones every module on its `develop` branch and skips the ones you already have. Add `--with-devenv`
(`-WithDevenv` in PowerShell) to also clone the observability stack.

## Building

| Command | What it builds |
|---|---|
| `./build.sh` | Every module, in dependency order |
| `./build.sh infrastructure` | `infrastructure`, everything that depends on it, and whatever those need to build |
| `./build.sh core --tests` | Same idea for `core`, running the tests |
| `./build.sh dsl --fast` | Only `dsl` and its dependents. Upstream modules must already be in `~/.m2` |
| `./build.sh dsl --no-install` | Package only, leaving `~/.m2` untouched |

By default modules are installed into your local `~/.m2` and tests are skipped. In PowerShell the options are
`-Tests`, `-Fast` and `-NoInstall`.

### Who needs a rebuild after a change

```
core ─┬─ crawler ──┐
      ├─ janitor ──┤
      ├─ mounter ──┼─> infrastructure ─> dsl ─> runtime
      └─ validator ─> ingester ─┘
core ─> watchdog     (nothing depends on it)
```

| If you change | Also rebuild |
|---|---|
| `core` | every other module |
| `crawler`, `janitor`, `mounter` | `infrastructure`, `dsl`, `runtime` |
| `validator` | `ingester`, `infrastructure`, `dsl`, `runtime` |
| `infrastructure` | `dsl`, `runtime` |
| `dsl` | `runtime` |
| `runtime`, `watchdog` | nothing |

`./build.sh <module>` already does this for you.

## Versions

Each module has its own version and depends on specific versions of the others, so when one module changes the modules
that depend on it change too. `bump.sh` does that for you:

```bash
./bump.sh janitor 1.3.0-SNAPSHOT     # set janitor's version, its dependents follow
./bump.sh janitor 1.3.0 --dry-run    # show the plan without touching any file
./bump.sh --check                    # verify every dependency matches the module's real version
```

The part that grows in the module you change (major, minor or patch) is the part that grows in each module that depends
on it. If `janitor` goes from 1.2.1 to 1.3.0 (a minor), `infrastructure` 1.3.0 becomes 1.4.0 and `runtime` 1.4.0 becomes
1.5.0, and their dependencies on `janitor` are rewritten to the new version. Pass `--patch`, `--minor` or `--major` to
force the part that grows in the dependents. Nothing is committed: review and commit each repository yourself.

`bump.sh` needs a POSIX shell, so on Windows use Git Bash or WSL. CI runs `./bump.sh --check` to catch versions that
drift apart.

## Releases

Releasing is done by GitHub Actions, not from anybody's machine, so it works the same for every developer. Go to
Actions, pick **Release** and run it.

1. The plan is the first job and it is also what a dry run shows (`dry_run` is on by default). A module is pending when
   the version of its `pom.xml` on `main`, without `-SNAPSHOT`, is not on Maven Central. The modules come out in
   dependency order, and a dependency on a version that is neither on Central nor released earlier in the plan is
   reported as a problem that stops the release.
2. Run it again with `dry_run` off. The reviewers of the `release` environment approve it, then each pending module in
   turn gets its own release workflow started, and the next one does not start until three things are true: the workflow
   finished successfully, Central shows the version, and Maven can resolve it from Central alone the way a consumer would.
3. The first failure stops the release. The modules that come after it are reported as not attempted, and the table at
   the end of the run shows how long each step took.

Nothing is waited for by guessing a time, it is polled with a growing pause. The same plan is available locally, and it
only reads public data:

```bash
python3 tools/release.py plan
```

To release, merge `develop` into `main` in each module that changed, then run the workflow. `release.py run` needs
`GH_TOKEN` to hold a token that can start workflows in the module repositories, which in the workflow is the
`RELEASE_TOKEN` secret. Publishing to Central cannot be undone, so a failed run is fixed by bumping the version and
releasing again, never by releasing the same one twice.

## Working in an IDE

Open `pom.xml` from this repository as a Maven project. The ten modules are imported together, so you can navigate and
refactor across them from a single window.

## GitHub Packages

The modules declare the `Spool-FRAMEWORK` GitHub Packages repository, which requires authentication even to read.
If a build reports `401 Unauthorized`, add a server with id `github` to `~/.m2/settings.xml`, using a personal access
token that has the `read:packages` scope:

```xml
<settings>
  <servers>
    <server>
      <id>github</id>
      <username>YOUR_GITHUB_USER</username>
      <password>YOUR_TOKEN</password>
    </server>
  </servers>
</settings>
```

After a failed download, Maven caches the failure for a while. To force a retry, pass `-U` through the `MAVEN_ARGS`
environment variable, which Maven 3.9 reads by itself:

```bash
MAVEN_ARGS=-U ./build.sh dsl
```

## Adding a module

Add a `<module>../name</module>` line to `pom.xml`. `bootstrap` reads the list from there, so nothing else needs to change.
