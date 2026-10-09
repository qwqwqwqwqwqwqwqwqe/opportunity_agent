"""Report prerequisites without changing the developer environment."""
from __future__ import annotations

import importlib.util
import shutil
import sys


def status(name: str, available: bool, detail: str = "") -> bool:
    print(f"[{'OK' if available else 'MISSING'}] {name}{': ' + detail if detail else ''}")
    return available


def main() -> int:
    python_ok = sys.version_info >= (3, 11)
    ok = status("Python 3.11+", python_ok, sys.version.split()[0])
    ok &= status("cargo", shutil.which("cargo") is not None)
    ok &= status("maturin", shutil.which("maturin") is not None)
    ok &= status("openjiuwenrust", importlib.util.find_spec("openjiuwenrust") is not None)
    status("pydantic", importlib.util.find_spec("pydantic") is not None)
    print("\nOffline demo requires Python + pydantic. Rust/LLM mode additionally requires the last three prerequisites.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
