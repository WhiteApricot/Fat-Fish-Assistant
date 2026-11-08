"""Verify Linux artifacts in a flat offline cache without modifying it."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "manifests"


def verify_cache(cache, manifests):
    seen = {}
    for manifest in manifests:
        for row in json.loads(manifest.read_text(encoding="utf-8")):
            name = row.get("file") or row["filename"]
            expected = row.get("sha256") or row["archive_sha256"]
            if not name or Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError(f"Invalid artifact name: {name}")
            if name in seen:
                if seen[name] != expected:
                    raise ValueError(f"Conflicting artifact hashes: {name}")
                continue
            with (cache / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError(f"SHA256 mismatch: {name}")
            seen[name] = expected
    return len(seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--profile", choices=("core", "napcat"), default="core")
    args = parser.parse_args()
    names = [MANIFESTS / "linux-x86_64" / f"{args.profile}.json"]
    if args.profile == "core":
        names.append(MANIFESTS / "build.json")
    try:
        count = verify_cache(args.cache, names)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Verified {count} {args.profile} artifacts")


if __name__ == "__main__":
    main()
