#!/usr/bin/env bash
set -e

# CapsuleerAI local startup script
# Runs the app directly without Docker — for local use with Ollama

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# .env setup
# ---------------------------------------------------------------------------

if [ ! -f ".env" ]; then
    echo "No .env found. Creating from .env.example..."
    cp .env.example .env
fi

# Read a value from .env
_env_get() {
    grep -E "^${1}=" .env | cut -d= -f2- | tr -d '[:space:]'
}

# Write or replace a key=value in .env
_env_set() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

# Auto-generate SECRET_KEY if it's still the placeholder
secret_key=$(_env_get SECRET_KEY)
if [ -z "$secret_key" ] || [ "$secret_key" = "change_this_to_a_long_random_string" ]; then
    new_key=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    _env_set SECRET_KEY "$new_key"
    echo "Generated new SECRET_KEY."
fi

# Prompt for EVE credentials if still placeholders
eve_id=$(_env_get EVE_CLIENT_ID)
if [ -z "$eve_id" ] || [ "$eve_id" = "your_eve_client_id" ]; then
    echo ""
    echo "EVE Online SSO credentials are required."
    echo "Create an app at https://developers.eveonline.com/ if you haven't already."
    echo "  Callback URL: http://localhost:8000/auth/callback"
    echo ""
    read -rp "  EVE Client ID:     " eve_id
    _env_set EVE_CLIENT_ID "$eve_id"
fi

eve_secret=$(_env_get EVE_CLIENT_SECRET)
if [ -z "$eve_secret" ] || [ "$eve_secret" = "your_eve_client_secret" ]; then
    read -rsp "  EVE Client Secret: " eve_secret
    echo ""
    _env_set EVE_CLIENT_SECRET "$eve_secret"
    echo "Credentials saved to .env."
fi

# Prompt for Anthropic API key if provider is set to anthropic but key is missing
llm_provider=$(_env_get LLM_PROVIDER)
if [ "${llm_provider,,}" = "anthropic" ]; then
    anthropic_key=$(_env_get ANTHROPIC_API_KEY)
    if [ -z "$anthropic_key" ] || [ "$anthropic_key" = "your_anthropic_api_key" ]; then
        echo ""
        echo "LLM_PROVIDER is set to 'anthropic' but ANTHROPIC_API_KEY is missing."
        read -rsp "  Anthropic API Key: " anthropic_key
        echo ""
        _env_set ANTHROPIC_API_KEY "$anthropic_key"
        echo "Anthropic API key saved to .env."
    fi
fi

# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install/update dependencies
pip install -q -r requirements.txt

# ---------------------------------------------------------------------------
# Ollama check
# ---------------------------------------------------------------------------

if [ "${llm_provider,,}" = "ollama" ]; then
    OLLAMA_URL=$(_env_get OLLAMA_BASE_URL)
    OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/v1}"
    BASE="${OLLAMA_URL%/v1}"
    if ! curl -sf "${BASE}/api/tags" > /dev/null 2>&1; then
        echo "WARNING: Ollama not responding at ${BASE}. Make sure 'ollama serve' is running."
    else
        MODEL=$(_env_get OLLAMA_MODEL)
        MODEL="${MODEL:-qwen3:32b}"
        if ! curl -sf "${BASE}/api/tags" | grep -q "\"${MODEL}\""; then
            echo "Model '${MODEL}' not found locally. Pulling now (this may take a while)..."
            ollama pull "${MODEL}"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

PIDFILE="$SCRIPT_DIR/capsuleerai.pid"
LOGFILE="$SCRIPT_DIR/capsuleerai.log"

# Stop any existing instance
if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE")
    if kill -0 "$old_pid" 2>/dev/null; then
        echo "Stopping existing instance (PID $old_pid)..."
        kill "$old_pid"
        sleep 1
    fi
    rm -f "$PIDFILE"
fi

echo ""
echo "Starting CapsuleerAI at http://localhost:8000"
echo "Logs: $LOGFILE"
echo "Stop: kill \$(cat capsuleerai.pid)  or  ./stop.sh"
echo ""

nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
APP_PID=$(cat "$PIDFILE")

# Wait up to 10 seconds for the app to confirm it's listening
echo -n "Waiting for app to start"
for i in $(seq 1 20); do
    sleep 0.5
    if grep -q "Application startup complete" "$LOGFILE" 2>/dev/null; then
        echo ""
        echo "✓ CapsuleerAI is running — PID $APP_PID"
        echo "  http://localhost:8000"
        echo ""
        echo "To watch logs:  tail -f $LOGFILE"
        echo "To filter errors:  tail -f $LOGFILE | grep -i 'error\|warning\|critical'"
        echo "To stop:  ./stop.sh"
        exit 0
    fi
    # Check if the process already died
    if ! kill -0 "$APP_PID" 2>/dev/null; then
        echo ""
        echo "✗ App failed to start. Last log output:"
        echo ""
        tail -20 "$LOGFILE"
        exit 1
    fi
    echo -n "."
done

echo ""
echo "✗ App did not confirm startup within 10 seconds."
echo "  Check the log for errors:  tail -30 $LOGFILE"
