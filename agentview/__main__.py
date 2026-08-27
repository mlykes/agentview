"""Allow `python3 -m agentview ...`.

The console script in pyproject.toml only exists after a `pip install`, which this
project deliberately does not require. This is the invocation that always works from
a plain checkout.
"""

import sys

from agentview.cli import main

if __name__ == "__main__":
    sys.exit(main())
