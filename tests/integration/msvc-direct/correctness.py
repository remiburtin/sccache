"""Check native MSVC direct hits and conservative fallbacks against cl.exe."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

from reproduce import require


SCENARIOS = (
    "flags", "forced-include", "include-env", "cl-env", "suffix-cl-env",
    "windows-paths", "basedirs", "show-includes", "source-dependencies",
    "show-includes-header", "show-includes-forced-include", "show-includes-include-env",
    "source-date", "source-time", "source-timestamp", "header-time",
    "source-import", "header-import", "clang-cl",
)


def evaluate(sccache, root, scenario):
    always_show_includes = scenario.startswith("show-includes-")
    input_scenario = scenario[len("show-includes-"):] if always_show_includes else scenario
    work = root / scenario
    work.mkdir()
    compiler = shutil.which("clang-cl.exe" if scenario == "clang-cl" else "cl.exe")
    require(compiler, f"Compiler unavailable for {scenario}")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("SCCACHE_") and key.upper() not in ("CL", "_CL_")
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_path = work / "server.log"
    (work / "config.toml").write_text("", encoding="utf-8")
    env.update(
        SCCACHE_DIR=str(work / "cache"),
        SCCACHE_CONF=str(work / "config.toml"),
        SCCACHE_CACHED_CONF=str(work / "cached-config.toml"),
        SCCACHE_DIRECT="1",
        SCCACHE_SERVER_PORT=str(port),
        SCCACHE_CLIENT_SIDE="0",
        SCCACHE_LOG="sccache=debug",
        SCCACHE_ERROR_LOG=str(log_path),
    )
    trees = [work / "tree one", work / "tree two"]
    for tree in trees:
        tree.mkdir()
        (tree / "value.h").write_text("#define VALUE 42\n", encoding="utf-8")
        (tree / "forced.h").write_text("#define FORCED 42\n", encoding="utf-8")
        source = '#include "value.h"\nint value() { return VALUE; }\n'
        if scenario in ("flags", "cl-env", "suffix-cl-env"):
            source = '#include "value.h"\nint value() { return SELECT; }\n'
        elif input_scenario == "forced-include":
            source = "int value() { return FORCED; }\n"
        elif input_scenario == "include-env":
            source = "#include <value.h>\nint value() { return VALUE; }\n"
        elif scenario in ("source-date", "source-time", "source-timestamp"):
            token = scenario[len("source-"):].upper()
            source += f"const char *stamp = __{token}__;\n"
        elif scenario == "header-time":
            (tree / "value.h").write_text(
                "#define VALUE 42\nconst char *stamp = __TIME__;\n", encoding="utf-8"
            )
        elif scenario == "source-import":
            source += '#if 0\n#import "untracked.tlb"\n#endif\n'
        elif scenario == "header-import":
            (tree / "value.h").write_text(
                '#define VALUE 42\n#if 0\n#import "untracked.tlb"\n#endif\n',
                encoding="utf-8",
            )
        (tree / "main.cpp").write_text(source, encoding="utf-8")
    includes = [work / "include one", work / "include two"]
    for index, directory in enumerate(includes):
        directory.mkdir()
        (directory / "value.h").write_text(f"#define VALUE {42 + index}\n", encoding="utf-8")
    if scenario == "basedirs":
        env["SCCACHE_BASEDIRS"] = ";".join(str(tree) for tree in trees)
        (trees[1] / "value.h").write_text("#define VALUE 43\n", encoding="utf-8")

    def run(label, command, cwd=work):
        (work / f"{label}.command.json").write_text(
            json.dumps([str(arg) for arg in command], indent=2), encoding="utf-8"
        )
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True)
        (work / f"{label}.stdout").write_bytes(result.stdout)
        (work / f"{label}.stderr").write_bytes(result.stderr)
        require(result.returncode == 0, f"{scenario}/{label} failed; inspect {work}")
        return result

    report, objects = [], []
    run("start", [sccache, "--start-server"])
    try:
        for index in range(4):
            changed = index >= 2
            tree = trees[int(changed)] if scenario == "basedirs" else trees[0]
            flags = ["/nologo", "/c", "/Brepro", "main.cpp", "/Fomain.obj"]
            if scenario == "flags":
                flags += [f"/DSELECT={43 if changed else 42}", "/UUNUSED"]
            elif input_scenario == "forced-include":
                flags += [f"/FI{tree / 'forced.h'}"]
            elif input_scenario == "include-env":
                env["INCLUDE"] = (
                    str(includes[int(changed)]) + ";" + os.environ.get("INCLUDE", "")
                )
            elif scenario in ("cl-env", "suffix-cl-env"):
                key = "CL" if scenario == "cl-env" else "_CL_"
                env[key] = f"/DSELECT#{43 if changed else 42}"
            elif scenario == "windows-paths":
                flags = [
                    "/nologo", "/c", "/Brepro", str(tree / "main.cpp"),
                    f"/Fo{tree / 'main.obj'}", f"/I{tree}",
                ]
            elif scenario == "show-includes" and index % 2:
                flags += ["/showIncludes"]
            elif scenario == "source-dependencies":
                flags += ["/sourceDependencies", "deps.json"]
            if always_show_includes:
                flags += ["/showIncludes"]
            if index == 2 and input_scenario not in (
                "flags", "include-env", "cl-env", "suffix-cl-env", "basedirs", "show-includes"
            ):
                header = tree / ("forced.h" if input_scenario == "forced-include" else "value.h")
                header.write_text(
                    header.read_text(encoding="utf-8").replace("42", "43"), encoding="utf-8"
                )
            time.sleep(2)
            (tree / "main.obj").unlink(missing_ok=True)
            (tree / "deps.json").unlink(missing_ok=True)
            before = log_path.stat().st_size
            result = run(f"build-{index}", [sccache, compiler, *flags], tree)
            objects.append((tree / "main.obj").read_bytes())
            log = log_path.read_bytes()[before:]
            (work / f"build-{index}.log").write_bytes(log)
            direct = scenario in (
                "flags", "forced-include", "include-env", "windows-paths"
            ) or always_show_includes
            direct = direct and index % 2 == 1
            if scenario == "show-includes":
                direct = index >= 2
                require(
                    (b"value.h" in result.stdout + result.stderr) == bool(index % 2),
                    "Incorrect /showIncludes replay",
                )
            if always_show_includes:
                header = b"forced.h" if input_scenario == "forced-include" else b"value.h"
                require(header in result.stdout + result.stderr, "Missing /showIncludes output")
                if input_scenario == "include-env":
                    require(
                        includes[int(changed)].name.encode() in result.stdout + result.stderr,
                        "Incorrect INCLUDE path in /showIncludes output",
                    )
            if scenario == "source-dependencies":
                require((tree / "deps.json").exists(), "Dependency output was not restored")
                dependency_bytes = (tree / "deps.json").read_bytes()
                (work / f"deps-{index}.json").write_bytes(dependency_bytes)
                require(
                    "value.h" in json.dumps(json.loads(dependency_bytes)),
                    "Missing header in dependency output",
                )
            row = {
                "stage": index,
                "direct_hits": log.count(b"Preprocessor cache hit:"),
                "preprocessor_commands": sum(
                    b"sccache::compiler::msvc" in line and b"preprocess: " in line
                    for line in log.splitlines()
                ),
            }
            report.append(row)
            require(row["direct_hits"] == int(direct), f"{scenario}: {row}")
            bypass = scenario in ("cl-env", "suffix-cl-env")
            require(
                row["preprocessor_commands"] == int(not direct and not bypass),
                f"{scenario}: {row}",
            )
            stats = run(f"stats-{index}", [sccache, "--show-stats", "--stats-format=json"])
            if scenario == "show-includes" or always_show_includes:
                stats = json.loads(stats.stdout)["stats"]
                hits = max(0, index - 1) if scenario == "show-includes" else (index + 1) // 2
                require(sum(stats["cache_hits"]["counts"].values()) == hits, str(stats))
                require(
                    sum(stats["cache_misses"]["counts"].values()) == index + 1 - hits,
                    str(stats),
                )
            # Same output pathname and flags avoid debug-path differences. Read
            # cached outputs first so the reference cannot repair a missing file.
            (tree / "main.obj").unlink()
            reference = run(f"reference-{index}", [compiler, *flags], tree)
            if scenario == "show-includes" or always_show_includes:
                require(
                    (result.stdout, result.stderr) == (reference.stdout, reference.stderr),
                    f"{scenario}: compiler output differs at stage {index}",
                )
            if scenario not in ("source-time", "header-time"):
                require(
                    objects[-1] == (tree / "main.obj").read_bytes(),
                    f"{scenario}: object differs from native compiler at stage {index}",
                )
        if scenario in (
            "flags", "forced-include", "include-env", "cl-env", "suffix-cl-env",
            "windows-paths", "basedirs", "source-dependencies", "clang-cl",
        ) or always_show_includes:
            require(
                objects[0] == objects[1] and objects[2] == objects[3],
                f"{scenario}: unchanged objects differ",
            )
            require(
                objects[0] != objects[2], f"{scenario}: changed input did not change the object"
            )
    finally:
        (work / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        run("stop", [sccache, "--stop-server"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sccache", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    require(os.name == "nt", "Run from a Windows MSVC Developer Command Prompt")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for scenario in SCENARIOS:
        print(f"Checking {scenario}", flush=True)
        evaluate(args.sccache.resolve(strict=True), root, scenario)


if __name__ == "__main__":
    main()
