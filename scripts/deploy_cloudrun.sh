#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:$PATH"

PROJECT_ID="${1:-thematic-land-511006-r1}"
REGION="${2:-asia-south1}"
SERVICE="inventory-qa-agent"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.cloudrun-env.yaml"

cd "$ROOT"

echo "==> Using project=$PROJECT_ID region=$REGION"
gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project="$PROJECT_ID"

echo "==> Building env file from .env"
python3 - <<'PY'
from pathlib import Path

env_path = Path(".env")
out_path = Path(".cloudrun-env.yaml")

keys = [
    "GEMINI_API_KEY",
    "GEMINI_API_KEYS",
    "MODEL_NAME",
    "MODEL_FALLBACKS",
    "AGENT_MAX_ITERATIONS",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_JWT_SECRET",
    "AUTH_DISABLED",
]

vals = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    k = k.strip()
    v = v.strip().strip('"').strip("'")
    if k in keys and v:
        vals[k] = v

# Force auth on in cloud
vals["AUTH_DISABLED"] = "false"

def yaml_escape(s: str) -> str:
    # double-quoted YAML string
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

lines = [f"{k}: {yaml_escape(v)}" for k, v in vals.items()]
out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote {out_path} with keys: {', '.join(vals)}")
PY

echo "==> Deploying to Cloud Run (source build)"
gcloud run deploy "$SERVICE" \
  --source "$ROOT" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --max-instances 3 \
  --min-instances 0 \
  --env-vars-file "$ENV_FILE" \
  --quiet

echo "==> Service URL"
gcloud run services describe "$SERVICE" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format='value(status.url)'

rm -f "$ENV_FILE"
echo "==> Done"
