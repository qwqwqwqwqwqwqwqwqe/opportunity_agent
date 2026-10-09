"""Isolated parser process, terminated by the parent on deadline/cancellation."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from .resume_parsers import parse_local

if __name__ == "__main__":
    try:
        print(json.dumps({"document": parse_local(Path(sys.argv[1])).model_dump(mode="json")}, ensure_ascii=True))
    except Exception as exc:
        # No tracebacks/file contents in transport or server logs.
        error = str(exc) if isinstance(exc, ValueError) else "文件无法解析"
        print(json.dumps({"error": error}, ensure_ascii=True))

