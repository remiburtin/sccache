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

After implementing the feature, run with `--expect direct`. Warm builds must
log a direct hit and no MSVC preprocessing command. Repeat with `--client-side`
to exercise the client pipeline; this mode captures stderr instead of setting
`SCCACHE_ERROR_LOG`, which would disable client-side mode.

The baseline expectation is a hypothesis, not a recorded Windows result.
This fixture has not yet been executed with native MSVC. It is a first
experiment, not the full correctness or performance matrix.

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

Production code is unchanged pending the requested native MSVC experiment.
Do not treat the `/EP` to `/E` switch alone as a complete correctness fix.

References:

- [Microsoft `/E` documentation](https://learn.microsoft.com/en-us/cpp/build/reference/e-preprocess-to-stdout)
- [Microsoft `/EP` documentation](https://learn.microsoft.com/en-us/cpp/build/reference/ep-preprocess-to-stdout-without-hash-line-directives)
- [Upstream direct-mode RFC #2766](https://github.com/mozilla/sccache/issues/2766)

The RFC proposes generic shadow-header probes. This checkout still uses the
include-only model, so MSVC would inherit the documented GCC/Clang shadow-header
limitation. Keep that work separate from the MSVC producer of dependencies.
