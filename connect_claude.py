#!/usr/bin/env python3
"""Adds the Form Studio connector (formstudio_mcp.py) to Claude Desktop's config, next to any connectors you already have.
Backs up the existing config first. Fully quit and reopen Claude Desktop afterwards.

  python connect_claude.py [path-to-python.exe]
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent
python = sys.argv[1] if len(sys.argv) > 1 else sys.executable
entry = {"command": python, "args": ["-B", str(root / "formstudio_mcp.py")]}

# Normal install: %APPDATA%\Claude. The Microsoft Store install keeps it under Packages.
dirs = []
if os.environ.get("APPDATA"):
    dirs.append(Path(os.environ["APPDATA"]) / "Claude")
if os.environ.get("LOCALAPPDATA"):
    packages = Path(os.environ["LOCALAPPDATA"]) / "Packages"
    if packages.exists():
        dirs += [p / "LocalCache" / "Roaming" / "Claude" for p in packages.glob("*Claude*")]
targets = [d for d in dirs if d.exists()] or dirs[:1]
if not targets:
    print("Couldn't find Claude Desktop's settings folder. Add the connector by hand (see README, 'Claude connector').")
    sys.exit(0)

for folder in targets:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "claude_desktop_config.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except Exception as e:
            print(f"Skipped {path}: couldn't read it ({e}). Add the connector by hand (README).")
            continue
    servers = data.setdefault("mcpServers", {})
    if servers.get("form-studio") == entry:
        print(f"Already connected: {path}")
        continue
    if path.exists():
        backup = path.with_name(f"claude_desktop_config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(path, backup)
        print(f"Backup: {backup}")
    servers["form-studio"] = entry
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Added the Form Studio connector to {path}")

print("Fully quit Claude Desktop (tray icon > Quit) and reopen it to load the connector.")
