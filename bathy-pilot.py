#!/usr/bin/env python3
"""Bathymetry pilot: one 1° square, Sentinel-2 + ICESat-2 to measured
error. See bathy/pilot.py and docs/BATHY.md. Needs bathy/requirements.txt."""
import sys

from bathy.pilot import main

if __name__ == "__main__":
    sys.exit(main())
