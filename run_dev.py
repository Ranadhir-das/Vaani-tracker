"""Development launcher for Vaani Desktop Tracker."""

import os
import sys
from pathlib import Path

# Add src to sys.path
project_root = Path(__file__).resolve().parent
src_dir = project_root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from vaani_tracker.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

