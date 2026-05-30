#!/usr/bin/env bash
# setup-keys.sh — interactively populate .env with optional API keys.
# Nothing is required. The app runs fully offline with sane fallbacks.

set -e
ROOT="/Users/japa/Desktop/Job-Hunt-Hacker"
ENV="$ROOT/.env"

if [ ! -f "$ENV" ]; then
  cp "$ROOT/.env.example" "$ENV"
  echo "Created $ENV from example."
fi

set_key() {
  local key="$1"
  local prompt="$2"
  local current="$(grep -E "^${key}=" "$ENV" | cut -d'=' -f2-)"
  echo ""
  echo "  $prompt"
  if [ -n "$current" ]; then
    echo "  current: ${current:0:8}..."
  fi
  read -r -p "  new value (blank = keep current): " val
  if [ -n "$val" ]; then
    # macOS sed -i requires '' arg; linux doesn't
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|^${key}=.*|${key}=${val}|" "$ENV"
    else
      sed -i "s|^${key}=.*|${key}=${val}|" "$ENV"
    fi
    echo "  saved."
  else
    echo "  kept."
  fi
}

echo "=================================================================="
echo " JOB-HUNT-HACKER · setup keys"
echo "=================================================================="
echo " All keys are OPTIONAL. The app works without any of them — it"
echo " uses template-based output and free public job sources."
echo "=================================================================="

set_key "ANTHROPIC_API_KEY"    "Anthropic API key (best resume/cover-letter quality)"
set_key "OPENAI_API_KEY"       "OpenAI API key (alternate LLM)"
set_key "SERPAPI_API_KEY"      "SerpApi key (enables Google Jobs adapter)"
set_key "GITHUB_TOKEN"         "GitHub token (raises ingest rate limit)"
set_key "GOOGLE_CLIENT_ID"     "Google OAuth client ID (for Gmail/Calendar — optional)"
set_key "GOOGLE_CLIENT_SECRET" "Google OAuth client secret"

echo ""
echo "Done. .env updated."
echo "Launch the app with: ./run.sh"
