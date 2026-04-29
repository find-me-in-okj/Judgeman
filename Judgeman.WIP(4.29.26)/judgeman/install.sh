#!/usr/bin/env bash
# install.sh — Judgeman installer for Linux / macOS
# Windows users: use install.bat or install.ps1 instead
set -euo pipefail
JUDGEMAN_SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.local/bin"

uninstall(){
    rm -f "$INSTALL_DIR/jm" "$INSTALL_DIR/jm-gui"
    rm -f "$HOME/.local/share/applications/judgeman.desktop" 2>/dev/null || true
    echo "Judgeman uninstalled."
    exit 0
}
[ "${1:-}" = "--uninstall" ] && uninstall

echo "Installing Judgeman..."

for pkg in click colorama flask; do
    python3 -c "import $pkg" 2>/dev/null || {
        echo "  Installing $pkg..."
        pip install "$pkg" --quiet --break-system-packages 2>/dev/null || \
        pip3 install "$pkg" --quiet 2>/dev/null || \
        python3 -m pip install "$pkg" --quiet
    }
done

mkdir -p "$INSTALL_DIR"

cat > "$INSTALL_DIR/jm" << LAUNCHER
#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '$JUDGEMAN_SRC/judgeman')
os.environ.setdefault('JUDGEMAN_HOME', os.path.expanduser('~/.judgeman'))
from cli import cli
cli()
LAUNCHER
chmod +x "$INSTALL_DIR/jm"
echo "  ✓ CLI: $INSTALL_DIR/jm"

cat > "$INSTALL_DIR/jm-gui" << LAUNCHER
#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '$JUDGEMAN_SRC/judgeman')
os.environ.setdefault('JUDGEMAN_HOME', os.path.expanduser('~/.judgeman'))
exec(open('$JUDGEMAN_SRC/jm-gui.py').read())
LAUNCHER
chmod +x "$INSTALL_DIR/jm-gui"
echo "  ✓ GUI: $INSTALL_DIR/jm-gui"

# Linux desktop entry
if [ -d "$HOME/.local/share/applications" ]; then
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/judgeman.desktop" << DESKTOP
[Desktop Entry]
Name=Judgeman
Comment=OSINT Analytical Reasoning Engine
Exec=python3 $JUDGEMAN_SRC/jm-gui.py
Icon=utilities-terminal
Terminal=false
Type=Application
Categories=Utility;
DESKTOP
    echo "  ✓ Desktop entry created"
fi

# macOS .app
if [[ "$(uname)" == "Darwin" ]]; then
    APP_DIR="$HOME/Applications/Judgeman.app/Contents/MacOS"
    mkdir -p "$APP_DIR"
    cat > "$APP_DIR/Judgeman" << MACOS
#!/usr/bin/env bash
exec python3 "$JUDGEMAN_SRC/jm-gui.py" "\$@"
MACOS
    chmod +x "$APP_DIR/Judgeman"
    echo "  ✓ macOS app: ~/Applications/Judgeman.app"
fi

if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo ""
    echo "  Add to your shell profile:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "  jm init \"Investigation Name\"   — CLI"
echo "  jm-gui                          — GUI"
echo ""
