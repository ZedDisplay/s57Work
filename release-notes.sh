#!/usr/bin/env bash
# release-notes.sh — the 'latest' release notes, generated from the R2
# listing so that charts and meshes from earlier runs stay linked.
# Used by build-charts.yml and build-mesh.yml; prints Markdown on stdout.
#
# Environment: ENDPOINT (R2 S3 endpoint), BUCKET (bucket[/folder]),
# R2_PUBLIC_URL (bare public hostname), AWS credentials for `aws s3 ls`.
set -euo pipefail

BUCKET="${BUCKET%/}"
PUB="${R2_PUBLIC_URL%/}"
# R2_BUCKET may include an in-bucket folder ("bucket/folder"); the public
# host serves from the bucket root, so append that folder to the link base.
case "$BUCKET" in
  */*) PUB="$PUB/${BUCKET#*/}" ;;
esac
MAX_ASSET_SIZE=2147483648   # GitHub hard limit: 2 GiB per release asset
DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)

listing=$(aws s3 ls --endpoint-url "$ENDPOINT" "s3://$BUCKET/charts/")

link_lines() {   # $1 = filename regex
  echo "$listing" | awk -v re="$1" '$4 ~ re {print $3, $4}' | sort -k2 \
    | while read -r size name; do
        note=""
        if [ "$size" -ge "$MAX_ASSET_SIZE" ]; then
          note=" *(R2 only — exceeds GitHub's 2 GiB asset limit)*"
        fi
        echo "- [$name]($PUB/charts/$name) ($(numfmt --to=iec "$size")B)$note"
      done
}

echo "Automated ENC chart build from NOAA S-57 data. Last update: $DATE."
echo
echo "### All charts on R2 (no size limit)"
echo
link_lines '\.(mbtiles|pmtiles)$'
echo
echo "Each district is published twice with identical tiles: \`.mbtiles\` (SQLite) and \`.pmtiles\` (single-file archive for HTTP range requests). Per-chart metadata: same URLs with \`.json\`."

meshes=$(link_lines '_mesh\.tar\.zst$')
if [ -n "$meshes" ]; then
  echo
  echo "### Navigation meshes"
  echo
  echo "$meshes"
  echo
  echo "Routing mesh of each district's charted water (triangles with depth, clearance and hazard values, chart lines as marked sides, chart points as marked corners; the tile format is named in each mesh's \`_mesh.json\`), built from the chart source cells by \`build-mesh.py\`. Before replacing a mesh that works, check that your router reads the format its \`_mesh.json\` names: a router that reads only \`WRPMESH1\` cannot open a \`WRPMESH2\` mesh. Unpack the archive and point a router at the folder, or mirror the unpacked folder from \`$PUB/charts/mesh/<district>/\`. Provenance and build checks: same URLs with \`_mesh.json\`; every mesh is listed in \`$PUB/charts/mesh/index.json\`."
fi
