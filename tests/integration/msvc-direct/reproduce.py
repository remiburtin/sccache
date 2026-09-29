"""Capture native MSVC preprocessing and check baseline or direct-cache behavior."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sccache", type=Path)
    parser.add_argument("--expect", choices=("baseline", "direct"), required=True)
    parser.add_argument("--client-side", action="store_true")
    parser.add_argument("--output-dir", type=Path, help="New directory for captured evidence")
    args = parser.parse_args()
    require(os.name == "nt", "Run in a Windows MSVC Developer Command Prompt.")
    compiler = shutil.which("cl.exe")
    require(compiler, "cl.exe must be on PATH.")
    sccache = args.sccache.resolve(strict=True)
    if args.output_dir:
        work = args.output_dir.resolve()
        work.mkdir(parents=True, exist_ok=False)
    else:
        work = Path(tempfile.mkdtemp(prefix="sccache-msvc-direct-"))
    print(f"Evidence directory: {work}", flush=True)
    fixture = Path(__file__).resolve().parent
    for name in ("main.cpp", "value.h"):
        shutil.copyfile(fixture / name, work / name)
    (work / "config.toml").write_text("", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("SCCACHE_") and key.upper() not in ("CL", "_CL_")
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env.update(
        SCCACHE_DIR=str(work / "cache"),
        SCCACHE_CONF=str(work / "config.toml"),
        SCCACHE_CACHED_CONF=str(work / "cached-config.toml"),
        SCCACHE_SERVER_PORT=str(port),
        SCCACHE_DIRECT="1",
        SCCACHE_CLIENT_SIDE="1" if args.client_side else "0",
        SCCACHE_LOG="sccache=debug",
    )
    server_log = work / "server.log"
    if not args.client_side:
        env["SCCACHE_ERROR_LOG"] = str(server_log)

    def run(label, command):
        (work / f"{label}.command.json").write_text(
            json.dumps([str(arg) for arg in command], indent=2), encoding="utf-8"
        )
        result = subprocess.run(command, cwd=work, env=env, capture_output=True)
        (work / f"{label}.stdout").write_bytes(result.stdout)
        (work / f"{label}.stderr").write_bytes(result.stderr)
        require(result.returncode == 0, f"{label} failed; inspect {work}")
        return result

    ep = run("native-EP", [compiler, "/nologo", "/EP", "main.cpp"])
    e = run("native-E", [compiler, "/nologo", "/E", "main.cpp"])
    marker = rb'^#line\s+\d+\s+"[^"\r\n]*value\.h"'
    require(not re.search(marker, ep.stdout, re.MULTILINE), "/EP emitted a header marker")
    require(re.search(marker, e.stdout, re.MULTILINE), "/E did not emit a header marker")

    report = []
    objects = []
    started = False
    try:
        run("start", [sccache, "--start-server"])
        started = True
        for index, label in enumerate(("cold", "warm", "header-changed", "warm-again")):
            if index == 2:
                (work / "value.h").write_bytes(b"#define VALUE 43\n")
            # Avoid the generic cache's new-file race guard on coarse timestamps.
            time.sleep(2)
            (work / "main.obj").unlink(missing_ok=True)
            before = server_log.stat().st_size if server_log.exists() else 0
            start = time.perf_counter()
            result = run(label, [sccache, compiler, "/nologo", "/c", "main.cpp", "/Fomain.obj"])
            elapsed = time.perf_counter() - start
            stats = run(f"{label}-stats", [sccache, "--show-stats", "--stats-format=json"])
            log = result.stderr if args.client_side else server_log.read_bytes()[before:]
            (work / f"{label}.log").write_bytes(log)
            objects.append((work / "main.obj").read_bytes())
            stats = json.loads(stats.stdout)["stats"]
            entries = list((work / "cache" / "preprocessor").rglob("*"))
            report.append({
                "stage": label,
                "seconds": elapsed,
                "preprocessor_commands": sum(
                    b"sccache::compiler::msvc" in line and b"preprocess: " in line
                    for line in log.splitlines()
                ),
                "direct_hits": log.count(b"Preprocessor cache hit:"),
                "entries": sum(path.is_file() for path in entries),
                "object_hits": sum(stats["cache_hits"]["counts"].values()),
                "object_misses": sum(stats["cache_misses"]["counts"].values()),
            })
    finally:
        (work / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if started:
            run("stop", [sccache, "--stop-server"])

    direct = args.expect == "direct"
    for index, row in enumerate(report):
        warm = index % 2 == 1
        require(row["preprocessor_commands"] == int(not (direct and warm)), str(row))
        require(row["direct_hits"] == int(direct and warm), str(row))
        require((row["entries"] > 0) == direct, str(row))
        require(row["object_hits"] == (index + 1) // 2, str(row))
        require(row["object_misses"] == index // 2 + 1, str(row))
    require(objects[0] == objects[1], "First warm object differs from the cold object")
    require(objects[2] == objects[3], "Second warm object differs from the changed object")
    require(objects[0] != objects[2], "Header edit did not change the compiled object")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
