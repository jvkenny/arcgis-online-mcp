#!/usr/bin/env python3
"""Entry point for running from a clone: `python3 server.py`. No install needed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arcgis_online_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
