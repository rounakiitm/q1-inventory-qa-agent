#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

PROJECT_ID="${1:-rounak-iitm}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

echo "Project=$PROJECT_ID number=$PROJECT_NUMBER"
echo "Compute SA=$COMPUTE_SA"
echo "Cloud Build SA=$CB_SA"

for ROLE in \
  roles/cloudbuild.builds.builder \
  roles/storage.admin \
  roles/artifactregistry.writer \
  roles/logging.logWriter \
  roles/run.admin \
  roles/iam.serviceAccountUser
do
  echo "Granting $ROLE -> $COMPUTE_SA"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="$ROLE" \
    --condition=None \
    --quiet >/dev/null
done

for ROLE in \
  roles/run.admin \
  roles/storage.admin \
  roles/artifactregistry.writer \
  roles/iam.serviceAccountUser \
  roles/logging.logWriter
do
  echo "Granting $ROLE -> $CB_SA"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${CB_SA}" \
    --role="$ROLE" \
    --condition=None \
    --quiet >/dev/null || true
done

echo "IAM grants done"
