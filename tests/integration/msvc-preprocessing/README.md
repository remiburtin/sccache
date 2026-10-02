# MSVC preprocessing fixtures

`msvc-19.44-E.stdout` and `msvc-19.44-EP.stdout` are byte-for-byte captures
of `main.cpp` from MSVC 19.44.35229.0 on Windows. They preserve CRLF and Windows path escaping
for the parser tests in `src/compiler/c.rs`; `value.h` is the included header.
Only `/E` contains header line markers.

Native cache regressions are in `tests/system.rs`. Run them from an MSVC
Developer Command Prompt with `cargo test --locked --test system msvc_direct`.
The ignored localization cases require the Spanish (`es-ES`) Visual Studio
language pack and a console:

```sh
cargo test --locked --test system msvc_localized_show_includes -- --ignored
```

The Windows MSRV CI job installs the language pack and requires these cases.
