import sys
from pathlib import Path

# Allow running the suite straight from a checkout, no install required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
