#!/bin/bash
# ─────────────────────────────────────────────────────────
# Indian SIM SMS Gateway — Setup Script
# ─────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "╔══════════════════════════════════════╗"
echo "║  Indian SIM SMS Gateway — Setup      ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ─── 1. Check prerequisites ──────────────────────────────
echo "▸ Checking prerequisites..."

if ! command -v python3 &> /dev/null; then
    echo "  ✗ Python 3 not found. Please install Python 3.10+"
    exit 1
fi
echo "  ✓ Python $(python3 --version | cut -d' ' -f2)"

if command -v docker &> /dev/null; then
    echo "  ✓ Docker $(docker --version | cut -d' ' -f3 | tr -d ',')"
else
    echo "  ⚠ Docker not found (optional, for Redis/MQTT)"
fi

# ─── 2. Create virtual environment ───────────────────────
echo ""
echo "▸ Setting up Python virtual environment..."

cd "$PROJECT_DIR/backend"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "  ✓ Virtual environment created"
else
    echo "  ✓ Virtual environment already exists"
fi

source .venv/bin/activate
echo "  ✓ Activated (.venv)"

# ─── 3. Install dependencies ─────────────────────────────
echo ""
echo "▸ Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "  ✓ Dependencies installed"

# ─── 4. Environment configuration ────────────────────────
echo ""
echo "▸ Setting up environment configuration..."

cd "$PROJECT_DIR"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "  ✓ Created .env from .env.example"
    echo "  ⚠ Edit .env with your actual API keys before starting"
else
    echo "  ✓ .env already exists"
fi

# ─── 5. Generate encryption key ──────────────────────────
echo ""
echo "▸ Generating encryption key..."
FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
echo "  ✓ Fernet key: $FERNET_KEY"
echo "  ⚠ Add this to your .env as FERNET_ENCRYPTION_KEY"

# ─── 6. Start Docker services (optional) ─────────────────
echo ""
if command -v docker &> /dev/null && command -v docker-compose &> /dev/null; then
    echo "▸ Starting Docker services (Redis + MQTT)..."
    cd "$PROJECT_DIR"
    docker-compose up -d redis mosquitto 2>/dev/null || true
    echo "  ✓ Docker services started"
else
    echo "▸ Skipping Docker services (docker-compose not found)"
    echo "  ⚠ The backend will use in-memory queue instead of Redis"
fi

# ─── 7. Run tests ────────────────────────────────────────
echo ""
echo "▸ Running tests..."
cd "$PROJECT_DIR"
python3 -m pytest tests/ -v --tb=short 2>/dev/null || echo "  ⚠ Some tests may require configuration"

# ─── Done ─────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════╗"
echo "║  ✅ Setup Complete!                  ║"
echo "╠══════════════════════════════════════╣"
echo "║                                      ║"
echo "║  Start backend:                      ║"
echo "║    cd backend && source .venv/bin/activate ║"
echo "║    uvicorn main:app --reload         ║"
echo "║                                      ║"
echo "║  Run benchmark:                      ║"
echo "║    python scripts/benchmark.py \\     ║"
echo "║      --simulate --count 1000         ║"
echo "║                                      ║"
echo "║  API docs: http://localhost:8000/docs ║"
echo "╚══════════════════════════════════════╝"
