"""Run explicitly: python tests/performance_scan.py --sizes 50 1000 10000 100000 1000000."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from test_engine import GeneratedTransport

from tgx.archive import verify_jsonl
from tgx.engine import ExportEngine
from tgx.storage import SQLiteStore


def peak_memory() -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "working",
                    "paged_peak",
                    "paged",
                    "nonpaged_peak",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.peak
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


async def main(sizes: list[int]):
    results = []
    for size in sizes:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStore(root / "state.sqlite3")
            start = time.monotonic()
            output = root / "archive.jsonl"
            try:
                await ExportEngine(GeneratedTransport(size), store, "main").export(42, output)
                result = verify_jsonl(output)
                assert result["messages"] == size
                peak = peak_memory()
                results.append(
                    {
                        "messages": size,
                        "seconds": round(time.monotonic() - start, 2),
                        "process_peak_bytes": peak,
                    }
                )
                print(json.dumps(results[-1]), flush=True)
            finally:
                store.close()
    if len(results) > 1:
        assert results[-1]["process_peak_bytes"] < results[0]["process_peak_bytes"] * 2 + 20_000_000


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[50, 1000, 10000, 100000, 1000000])
    asyncio.run(main(parser.parse_args().sizes))
