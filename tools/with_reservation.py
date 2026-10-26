#!/usr/bin/env python3
"""Run a bounded AP command only after HA grants an exclusive reservation.

A busy/unreachable HA fails closed (exit 75), without starting the AP command.
Renewals use the same authenticated HA user and opaque token. If renewal fails,
terminate the process group while the old lease is still valid. A failed command
retains its lease until expiry: uncertain logout must not immediately allow HA.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from ha_client import HAClient, HAError


def run(client, command, timeout=240, release_codes=(0,), clock=time.monotonic, sleep=time.sleep):
    if not command or not 1 <= timeout <= 600:
        raise ValueError("Command timeout must be 1..600 seconds")
    started = clock()
    ticket = client.reservation("acquire", duration=900)
    token = ticket["token"]
    # The local deadline starts BEFORE acquire, avoiding network transit extension.
    deadline = started + timeout
    renewal_at = clock() + 30
    proc = None
    failed = False
    old_handlers = {}

    def stop(_signal=None, _frame=None):
        nonlocal failed
        failed = True
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, stop)
        # No shell and no HA token in child arguments or environment.
        proc = subprocess.Popen(command, start_new_session=True)
        while proc.poll() is None:
            now = clock()
            if now >= deadline or failed:
                stop()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                break
            if now >= renewal_at:
                try:
                    client.reservation("renew", token=token, duration=900)
                except Exception:
                    stop()
                    continue
                renewal_at = clock() + 30
            sleep(0.25)
        code = proc.returncode
        if not failed and code in release_codes:
            client.reservation("release", token=token)
        return 75 if failed else code
    except BaseException:
        stop()
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        raise
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--release-codes", default="0")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        result = run(HAClient(args.config), command, args.timeout, tuple(map(int, args.release_codes.split(","))))
    except HAError as err:
        print(str(err) + "; AP command not started or stopped.", file=sys.stderr)
        result = 75
    except ValueError, OSError:
        print("Reservation command failed; check local configuration.", file=sys.stderr)
        result = 75
    return result


if __name__ == "__main__":
    sys.exit(main())
