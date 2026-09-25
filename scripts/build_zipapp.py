#!/usr/bin/env python3
"""Build a dependency-free executable archive with Python's standard library."""
import shutil
import tempfile
import zipapp
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "hostdelta.pyz"
destination.parent.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as directory:
    stage = Path(directory)
    shutil.copytree(root / "src/hostdelta", stage / "hostdelta", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (stage / "__main__.py").write_text("from hostdelta.cli import main\nraise SystemExit(main())\n")
    zipapp.create_archive(stage, destination, interpreter="/usr/bin/env python3", compressed=True)
destination.chmod(0o755)
print(destination)
