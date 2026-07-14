"""Ensure the repo root is importable so `from fetchforge import server` works
whether or not the package is pip-installed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
