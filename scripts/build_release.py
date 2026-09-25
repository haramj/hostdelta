#!/usr/bin/env python3
"""Build local release artifacts and checksums. Never publish or upload anything."""

import gzip
import hashlib
import re
import subprocess
import sys
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = re.search(r'__version__ = "([^"]+)"', (root / "src/hostdelta/__init__.py").read_text())[1]
subprocess.run([sys.executable, str(root / "scripts/build_zipapp.py")], check=True)
dist = root / "dist"
archive = dist / f"hostdelta-{version}.tar.gz"
allowed = {"src", "tests", "examples", "docs", "schemas", "scripts", "bin", ".github"}
top = {"pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "AGENTS.md", ".gitignore"}
with archive.open("wb") as stream, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as zipped, tarfile.open(fileobj=zipped, mode="w") as tar:
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if not file.is_file() or file.is_symlink():
            continue
        if relative.parts[0] not in allowed and str(relative) not in top:
            continue
        if any(part == "__pycache__" or part.endswith(".egg-info") for part in relative.parts) or file.suffix == ".pyc":
            continue
        info = tar.gettarinfo(str(file), arcname=f"hostdelta-{version}/{relative.as_posix()}")
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        with file.open("rb") as content:
            tar.addfile(info, content)
artifacts = [dist / "hostdelta.pyz", archive]
wheel = dist / f"hostdelta-{version}-py3-none-any.whl"
if wheel.exists():
    artifacts.append(wheel)
manifest = dist / "SHA256SUMS"
manifest.write_text("".join(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n" for file in artifacts))
print("\n".join(str(p) for p in [*artifacts, manifest]))
