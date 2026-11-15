import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core.legal_agent import run  # noqa: E402
from core.logger import setup_logging  # noqa: E402

if __name__ == "__main__":
    setup_logging()
    run()
