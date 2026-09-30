# Native MSVC direct-cache experiment

Run from a Windows MSVC Developer Command Prompt with Python 3.8 or newer and a
built sccache executable:

```powershell
python tests/integration/msvc-direct/reproduce.py target/debug/sccache.exe --expect baseline
```

The experiment captures native `/EP` and `/E` stdout as bytes, verifies the
presence of `value.h` line markers only with `/E`, then compiles four times:
cold, unchanged, header changed from 42 to 43, and unchanged again. It removes
only the object between compilations. Each run uses a fresh temporary cache,
configuration and server port; the evidence directory is printed and retained.

`report.json` records object-cache statistics, direct-hit log counts,
preprocessing command log counts, preprocessor-entry file counts and elapsed
time. The script checks object restoration and that the header change produces
a different object. Raw logs and command lines are retained for inspection.
Counts refer to sccache's preprocessing command logs, not OS process tracing.
Use `--output-dir PATH` to select a new evidence directory, for example when
collecting CI artifacts. Existing directories are rejected to avoid cache reuse.

The [MSVC direct-cache workflow](../../../.github/workflows/msvc-direct.yml)
runs with `--expect direct` on `windows-2022`, in both server and client-side modes, when
the experiment or compiler code is pushed to `feature/msvc-preprocessor`.
It builds with local storage only and uploads `msvc-direct-evidence`, including
raw `/E` and `/EP` output, per-build logs, statistics and compiler version.
Artifacts are uploaded on failure too, so unexpected behavior can be inspected.
The workflow also supports manual dispatch once it exists on the default branch.

Run with `--expect direct`. Warm builds must
log a direct hit and no MSVC preprocessing command. Repeat with `--client-side`
to exercise the client pipeline. This checkout disables client-side mode when
`SCCACHE_LOG` is present, so the client experiment leaves logging unset and
reports `null` for preprocessing-command and direct-hit counts. It checks cache
entries, object-cache statistics, restoration and header invalidation; it does
not prove preprocessing was skipped. The server experiment provides log-based
preprocessing counts.

## Native baseline results

[Run 36524996678](https://github.com/remiburtin/sccache/actions/runs/36524996678)
tested commit `33fe4cf` on Windows Server 2022 with MSVC `19.44.35229.0`.
The server experiment passed: all four builds ran preprocessing, both warm
builds hit the object cache, and no preprocessor-cache entries were written.
Changing `VALUE` from 42 to 43 produced a cache miss and a different object.

The captured `/E` output includes the header's absolute path with escaped
backslashes; `/EP` has no line markers. `msvc-19.44-E.stdout` and
`msvc-19.44-EP.stdout` preserve those captures byte for byte, including CRLF.
The parser tests in `src/compiler/c.rs` use these captures; the `/E` test runs
on Windows because it checks Windows filesystem path semantics.

That run's client step failed its log-count assertion: setting `SCCACHE_LOG`
had silently selected server-side execution. Those results do not validate
client-side mode. The reproducer now removes logging for client-side runs as
described above. This remains a first experiment, not the full correctness or
performance matrix.

## Investigation checkpoint

Initial checkout: `https://github.com/remiburtin/sccache.git`, branch
`feature/msvc-preprocessor`, HEAD
`8396f0209d74d496b7cb27cdf323cd3ff8d4a291`. A live `git ls-remote` query returned
the same SHA for `mozilla/sccache` main. The working tree was clean.

At that revision:

- `c.rs` permits MSVC through the generic preprocessor-cache pipeline.
- `Msvc::preprocess` ignores the mode argument. Ordinary local preprocessing
  uses `-EP`, for both native MSVC and clang-cl.
- The generic parser recognizes `#line`; an existing synthetic unit test,
  `test_process_preprocessed_file_line_directive`, checks header collection.
  Actual MSVC output, Windows escaping and encoding still require validation.
- Entries with no included files are not stored. Leave this generic limitation
  unchanged in the initial MSVC patch.
- The direct-key environment allowlist omits `INCLUDE`, `CL` and `_CL_`.
  MSVC argument parsing also ignores its environment parameter. These require
  invalidation coverage or conservative fallback before enabling direct hits.
- `/sourceDependencies` lives in `depfile`, is emitted during preprocessing,
  and is absent from cached `outputs`. Skipping preprocessing needs conservative
  fallback or explicit artifact preservation. `/showIncludes` is a dependency
  argument, which is absent from the direct-key argument list; test requests
  both with and without the flag against the same cache.
- Source time-macro hashing explicitly rejects `__TIME__`, but does not fold
  date/timestamp state into that source digest. Header date/timestamp handling
  exists separately. Do not assume all three source macros are safe.
- PCH and module flags already have rejection paths. `#import` has optional
  `.tlh`/`.tli` outputs but no demonstrated tracking of the original type library.
- The existing debug command is `--debug-preprocessor-cache`. At this revision
  it reads the platform default cache directory, ignoring the custom
  `SCCACHE_DIR`; this experiment counts entries in its isolated cache directly.

## Implementation under test

The native MSVC path now propagates the direct-mode flag and uses `/E` when it
needs line markers. Clang-cl stays on its existing preprocessing path and is
explicitly excluded from direct hits, including distributed/profile invocations
that happen to emit line markers.

`INCLUDE` is part of the direct key (case-insensitive environment names on
Windows). Nonempty `CL` or `_CL_` bypass caching entirely: merely hashing these
strings cannot account for hidden response files, PCH/module inputs or outputs.
`/sourceDependencies` continues to preprocess each time because its JSON is not
a cached artifact. `/showIncludes` supports direct hits using `/E` line markers
for header discovery. It distinguishes both manifest and object-cache keys so a
cached compilation without include output cannot answer a request that needs it.
The object cache replays the original stdout and stderr. Existing PCH/module
rejection remains in place.

Source time macros now disable the generic direct key unless explicitly ignored.
For MSVC, source/header scans additionally reject time macros, potential `import`
tokens, trigraph splices and UTF-16 content, including when time macros are
configured to be ignored. This deliberately allows false positives. It handles
imports in transitive headers before storing a manifest; it does not implement
type-library dependency tracking. MSVC direct hits are also disabled when
basedirs are configured, since manifests still store absolute header paths and
cannot safely be shared between relocated checkouts.

`correctness.py` runs a separate server-mode matrix against uncached compiler
invocations. It checks `/D` and `/U`, `/FI` header changes, `INCLUDE` search-path
changes, `CL` and `_CL_` fallback, absolute Windows paths with spaces, basedir
relocation with the old tree still present, toggling `/showIncludes` against the
same cache, `/showIncludes` with header edits, `/FI`, and `INCLUDE` changes, and
deletion/regeneration of `/sourceDependencies` JSON. The `/showIncludes` cases
check object hits and compare `/showIncludes` stdout/stderr byte-for-byte with
native output. Ordinary output without `/showIncludes` is compared after CRLF
to LF conversion to match the ANSI filter. Warm output must match cold sccache
output byte-for-byte. Debug logging stays enabled only in the server
so client log messages cannot contaminate the compiler output comparison.
Language scenarios alternate English (`VSLANG=1033`) and Spanish (`VSLANG=3082`)
in both orders, exercise an unset client `VSLANG` against a server started in
Spanish, and repeat with direct mode disabled. They require distinct localized
output, including non-ASCII prefix bytes, unchanged objects, and separate cold
misses followed by warm hits. Install the `es-ES` Visual Studio language pack
before running the matrix; the workflow installs it and does not silently skip
localization coverage when resources are missing.
Code-page scenarios keep Spanish selected while switching the client console
between UTF-8 and CP850 in both orders, including with direct mode disabled.
They compare native output byte-for-byte and require cache hits across code-page
changes. Reports record the client console code page alongside `VSLANG`.
It also checks conservative source/header time-macro and inactive `#import` fallbacks,
and runs clang-cl separately. It compares restored objects with native compiler
output except for `__TIME__` cases, where the clock can change between commands.
The inactive imports test conservative detection without requiring a type library;
they do not validate real COM-generated artifacts. Unicode and UNC path coverage
remain follow-up work.

The workflow now requires the four-stage direct-hit sequence and this matrix.
[Windows run 36622804614](https://github.com/remiburtin/sccache/actions/runs/36622804614)
passed on commit `b6ee6f1` with Rust 1.91.0 and MSVC 19.44.35229.0. The
server report records one preprocessing command on the cold build, a direct hit
with no preprocessing on the warm build, one preprocessing command and an object
miss after the header edit, then another direct hit with no preprocessing.
The client report records one preprocessor-cache entry, object restoration and
header invalidation; it cannot count preprocessing commands because logging is
disabled in client-side mode. All 16 matrix reports show the expected direct
hits or conservative fallbacks, and the workflow step passed its native object
comparisons. The evidence artifact is available from the linked run.

Local validation on macOS with Rust 1.98.1 passed `cargo fmt -- --check`, the
`AGENTS.md` clippy command, and `cargo test --locked --lib --bins --tests`
(558 passed, one ignored OAuth test; CUDA cases skipped without a compiler).
The full suite required execution outside the sandbox for local sockets and
system access. The local-storage-only MSVC tests and mocked direct-cache
pipeline also passed. Python scripts passed syntax compilation.

## Correctness scope

Shadow-header probes remain separate work. This implementation tracks files
that were actually included. Creating a previously absent header earlier on an
unchanged include search path can still return a stale direct hit, as documented
for generic direct mode. The `INCLUDE` test changes the search-path string; it
does not close this gap. This patch therefore does **not** establish the full
invariant that every change to a compiler-visible input invalidates direct hits.

References:

- [Microsoft `/E` documentation](https://learn.microsoft.com/en-us/cpp/build/reference/e-preprocess-to-stdout)
- [Microsoft `/EP` documentation](https://learn.microsoft.com/en-us/cpp/build/reference/ep-preprocess-to-stdout-without-hash-line-directives)
- [Microsoft environment option syntax](https://learn.microsoft.com/en-us/cpp/build/reference/cl-environment-variables)
- [Upstream direct-mode RFC #2766](https://github.com/mozilla/sccache/issues/2766)

The RFC proposes generic shadow-header probes. This checkout still uses the
include-only model, so MSVC would inherit the documented GCC/Clang shadow-header
limitation. Keep that work separate from the MSVC producer of dependencies.
