"""TEMPORARY. Start the managed worker with probes in an isolated instance."""
import argparse
import asyncio
import json
from pathlib import Path
import runpy
import sys

from probe import Writer, install

ASTRBOT_COMMIT = "f61a7f42696da6df25dc7a249ec9b8d54ed9da18"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, required=True)
    args = parser.parse_args()
    instance = args.instance.resolve()
    local_root = args.local_root.resolve(strict=True)
    if not instance.is_relative_to(local_root) or instance == local_root:
        parser.error("Use a dedicated instance below the local test root.")
    private = instance / "private"
    marker = private / "ffa-lark-latency"
    if not marker.is_file() or not marker.resolve().is_relative_to(instance):
        parser.error("Create the opt-in private/ffa-lark-latency marker inside the test instance.")
    worker = args.worker.resolve(strict=True)
    if worker.name != "run_chat.py" or worker.parent.name != "fat_fish_management":
        parser.error("Use the FFA managed chat worker.")
    try:
        metadata = json.loads((instance / "instance.json").read_text(encoding="utf-8"))
        release = Path(metadata["release"]).resolve(strict=True)
        sources = json.loads((release / "release.json").read_text(encoding="utf-8"))["external_sources"]
        supported = any(row["name"] == "astrbot" and row["commit"] == ASTRBOT_COMMIT for row in sources)
    except (OSError, KeyError, TypeError, ValueError):
        parser.error("Use an initialized test instance with a managed release.")
    if not release.is_relative_to(local_root) or not supported:
        parser.error("Use an independent test release with the supported AstrBot commit.")
    log = private / "latency-traces.jsonl"
    if not log.resolve().is_relative_to(instance):
        parser.error("The trace log must stay inside the test instance.")
    writer = Writer(log)
    normal_run = asyncio.run

    def run_with_probe(coroutine):
        asyncio.run = normal_run

        async def observed():
            try:
                install(writer)
            except BaseException:
                coroutine.close()
                raise
            return await coroutine

        return normal_run(observed())

    asyncio.run = run_with_probe
    sys.argv = [str(worker), "--instance", str(instance)]
    try:
        runpy.run_path(str(worker), run_name="__main__")
    finally:
        asyncio.run = normal_run
        writer.close()


if __name__ == "__main__":
    main()
