#!/bin/bash
# Autonomous test loop for the OrcaSlicer plugin
set -e
BEHEMOTH="behemoth"
PLUGIN_SRC="/home/tommaso/projects/Orca_plugin_Search_Engine/search_engine.py"
PLUGIN_DST="/home/tommaso/.config/OrcaBelt2608-test/orca_plugins/search_engine/search_engine.py"
LOG_DIR="/home/tommaso/.config/OrcaBelt2608-test/log"

echo "=== AUTONOMOUS PLUGIN TEST ==="
echo ""

# Step 1: Compile
echo "[1/4] Compiling..."
python3 -m py_compile "$PLUGIN_SRC" || { echo "COMPILE FAILED"; exit 1; }
echo "  OK"

# Step 2: Deploy
echo "[2/4] Deploying..."
scp "$PLUGIN_SRC" "$BEHEMOTH:$PLUGIN_DST" 2>/dev/null || { echo "DEPLOY FAILED"; exit 1; }
echo "  OK"

# Step 3: Restart OrcaBelt
echo "[3/4] Restarting OrcaBelt..."
ssh "$BEHEMOTH" "pkill -f OrcaBelt2608" 2>/dev/null || true
sleep 3
ssh "$BEHEMOTH" "DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 nohup OrcaBelt2608 --datadir /home/tommaso/.config/OrcaBelt2608-test > /tmp/orca_test.log 2>&1 &" 2>/dev/null
sleep 8

# Step 4: Check logs
echo "[4/4] Checking logs..."
LOGFILE=$(ssh "$BEHEMOTH" "ls -t $LOG_DIR/debug_*.log.0 2>/dev/null | head -1")
if [ -z "$LOGFILE" ]; then
    echo "  No log file found"
    exit 1
fi

# Check for plugin load success
PLUGIN_OK=$(ssh "$BEHEMOTH" "strings '$LOGFILE' 2>/dev/null | grep -c 'SUCCESS plugin=search_engine'")
PLUGIN_ERR=$(ssh "$BEHEMOTH" "strings '$LOGFILE' 2>/dev/null | grep -c 'UNLOADED plugin=search_engine\|exception.*search_engine\|FAILED.*search_engine'")

echo "  Plugin load: $PLUGIN_OK successes, $PLUGIN_ERR errors"

if [ "$PLUGIN_ERR" -gt 0 ]; then
    echo ""
    echo "ERRORS:"
    ssh "$BEHEMOTH" "strings '$LOGFILE' 2>/dev/null | grep 'search_engine'"
    exit 1
fi

if [ "$PLUGIN_OK" -eq 0 ]; then
    echo "  Plugin not loaded yet (OrcaBelt still starting?)"
    exit 1
fi

echo ""
echo "=== ALL CHECKS PASSED ==="
echo "Plugin deployed and loaded successfully."
echo "Open OrcaBelt -> Plugins -> 3D Model Search -> search 'benchy'"
echo ""
echo "What should work:"
echo "  - Search returns results (~126 across 4 platforms)"
echo "  - Cards show images and license badges"
echo "  - Click card -> detail panel with model info"
echo "  - Click Download -> downloads file to plugin dir"
echo ""
echo "Log: ssh behemoth 'tail -f /tmp/orca_test.log'"
