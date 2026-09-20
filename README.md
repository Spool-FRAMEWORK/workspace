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
| `./build.sh infrastructure` | `infrastructure` plus everything it depends on and everything that depends on it |
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
