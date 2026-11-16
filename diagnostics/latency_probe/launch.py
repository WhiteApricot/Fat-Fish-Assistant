"""TEMPORARY. Start the managed worker with probes in an isolated instance."""
import argparse
import asyncio
from pathlib import Path
import runpy
import sys

from probe import Writer, install


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
    if not (private / "ffa-lark-latency").is_file():
        parser.error("Create the opt-in private/ffa-lark-latency marker in the test instance.")
    worker = args.worker.resolve(strict=True)
    writer = Writer(private / "latency-traces.jsonl")
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
