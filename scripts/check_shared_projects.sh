#!/usr/bin/env bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
set +e

echo "=== account ==="
gcloud auth list
echo
echo "=== all accessible projects ==="
gcloud projects list --format='table(projectId,name,projectNumber,lifecycleState)'
echo
echo "=== billing accounts ==="
gcloud billing accounts list
echo
echo "=== per-project billing + Cloud Run API ==="
while read -r pid; do
  [ -z "$pid" ] && continue
  billing=$(gcloud billing projects describe "$pid" --format='value(billingEnabled)' 2>&1)
  run_api=$(gcloud services list --enabled --project="$pid" --filter='config.name:run.googleapis.com' --format='value(config.name)' 2>&1)
  perms=$(gcloud projects get-iam-policy "$pid" --flatten='bindings[].members' --filter="bindings.members:user:$(gcloud config get-value account 2>/dev/null)" --format='value(bindings.role)' 2>&1 | tr '\n' ',' )
  echo "PROJECT=$pid"
  echo "  billingEnabled=$billing"
  echo "  run.googleapis.com=${run_api:-not-enabled-or-no-access}"
  echo "  your_roles=${perms:-unknown}"
  echo
done < <(gcloud projects list --format='value(projectId)')
