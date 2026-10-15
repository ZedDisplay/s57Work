#!/usr/bin/env python3
"""Pilot depths to a chart-shaped, zoom-aware .mbtiles. See bathy/chart.py
and docs/BATHY.md. Needs bathy/requirements.txt, GDAL and tippecanoe."""
import sys

from bathy.chart import main

if __name__ == "__main__":
    sys.exit(main())
