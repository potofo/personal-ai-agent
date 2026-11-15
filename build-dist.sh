#!/bin/sh
# Builds the Coder template upload bundle into dist/.
#   dist/personal-agent-template/      -> coder templates push personal-agent -d dist/personal-agent-template
#   dist/personal-agent-template.tar   -> Coder web UI: Templates > Create template > Upload template
#   dist/personal-agent-template.zip   -> same, zip format
set -eu
cd "$(dirname "$0")"
NAME=personal-agent-template
OUT=dist/$NAME
rm -rf "$OUT" && mkdir -p "$OUT/build/app/static"
cp terraform/main.tf terraform/README.md "$OUT"/
# App source, built into an image on the Coder host when the template variable "image" is empty.
cp Dockerfile entrypoint.sh requirements.txt "$OUT/build/"
cp app/*.py "$OUT/build/app/"
cp app/static/index.html "$OUT/build/app/static/"
cp -r app/default_skills "$OUT/build/app/"
find "$OUT/build" -name __pycache__ -prune -exec rm -rf {} +
printf '__pycache__\n*.pyc\n' > "$OUT/build/.dockerignore"
cat > dist/variables.example.yaml <<'YAML'
# coder templates push personal-agent -d dist/personal-agent-template --variables-file dist/variables.example.yaml
image: ""   # empty = build on the Coder host from build/; or set a registry image
azure_openai_endpoint: "https://YOUR-RESOURCE.openai.azure.com"
realtime_deployment: "gpt-realtime-2.1-mini"
app_subdomain: "false"
YAML
# Archives contain the template files at the root (what Coder expects).
tar -C "$OUT" --owner=0 --group=0 --numeric-owner -cf "dist/$NAME.tar" .
python3 -I - "$OUT" "dist/$NAME.zip" <<'PY'
import sys, zipfile, os
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for f in sorted(files):
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, src))
PY
(cd dist && sha256sum "$NAME.tar" "$NAME.zip" > SHA256SUMS)
echo "built:"; ls -l dist dist/$NAME
