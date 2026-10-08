#!/usr/bin/env bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
echo "=== billing accounts ==="
gcloud billing accounts list
echo "=== projects billing ==="
for p in \
  project-063d9736-22c1-428e-988 \
  project-0d002e38-4a18-4e31-bd4 \
  project-29279241-2cf3-4a0d-9ee \
  project-6c34a4ac-0cc9-427c-b95 \
  project-fcc8710b-fd06-4da4-94d \
  thematic-land-511006-r1
do
  echo -n "$p: "
  gcloud billing projects describe "$p" --format='value(billingEnabled,billingAccountName)' 2>&1 || true
done
