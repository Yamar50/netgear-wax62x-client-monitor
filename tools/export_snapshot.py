#!/usr/bin/env python3
"""Read HA's cached snapshot and write it atomically; never poll an AP."""

import argparse
import json
import os
from pathlib import Path
from ha_client import HAClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    data = HAClient(args.config).request("/api/netgear_wax/snapshot")
    if data.get("schema_version") != 1 or not isinstance(data.get("clients"), dict):
        raise ValueError("Unsupported snapshot")
    target = Path(args.output)
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
