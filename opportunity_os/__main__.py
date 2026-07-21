"""`python -m opportunity_os <command>` — same CLI as run.py.

The audit mandate names `python -m opportunity_os diagnose_sources` explicitly,
so the package is directly runnable; run.py stays the doc'd path.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from run import main  # noqa: E402

if __name__ == "__main__":
    main()
