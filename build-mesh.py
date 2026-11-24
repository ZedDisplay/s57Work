#!/usr/bin/env python3
"""Build the navigation mesh of a district from its chart source cells.
See mesh/cli.py and docs/MESH.md. Needs the packages in
mesh/requirements.txt and a C++ compiler with CGAL, GMP and MPFR."""
import sys

from mesh.cli import main

if __name__ == "__main__":
    sys.exit(main())
