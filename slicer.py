"""Slice print pieces with Bambu Studio's command line using its own X2D system presets. Standard library only.

Bambu's system presets inherit from parents and include g-code templates; the command line wants complete
settings, so they are flattened here first.
"""
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path

MACHINE = "Bambu Lab X2D 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL X2D"
MATERIALS = {  # Form Studio name -> (Bambu filament preset base name, density g/cm3, typical price $/kg)
    "PLA": ("Bambu PLA Basic", 1.24, 20), "PLA Matte": ("Bambu PLA Matte", 1.24, 20), "PLA Tough": ("Bambu PLA Tough", 1.24, 25),
    "PETG": ("Bambu PETG HF", 1.27, 20), "ABS": ("Bambu ABS", 1.04, 20), "ASA": ("Bambu ASA", 1.07, 25),
    "TPU": ("Bambu TPU 95A HF", 1.21, 35), "PLA-CF": ("Bambu PLA-CF", 1.22, 30), "PETG-CF": ("Bambu PETG-CF", 1.25, 30),
    "PA6-CF": ("Bambu PA6-CF", 1.13, 60)}
META = {"name", "inherits", "include", "instantiation", "setting_id", "from", "type", "filament_id", "base_id"}


def profiles_dir(exe):
    return Path(exe).resolve().parent / "resources" / "profiles" / "BBL"


class Presets:
    def __init__(self, root: Path):
        self.root = root
        index = json.loads((root.parent / "BBL.json").read_text(encoding="utf-8"))
        self.paths = {}
        for kind in ("machine_list", "process_list", "filament_list"):
            for item in index.get(kind, []):
                self.paths[item["name"]] = root / item["sub_path"]

    def raw(self, name):
        path = self.paths.get(name)
        if not path or not path.is_file():
            raise ValueError(f"Bambu Studio preset '{name}' wasn't found. Update Bambu Studio.")
        return json.loads(path.read_text(encoding="utf-8"))

    def flat(self, name, depth=0):
        if depth > 20:
            raise ValueError("Preset inheritance is too deep")
        own = self.raw(name)
        merged = self.flat(own["inherits"], depth + 1) if own.get("inherits") else {}
        for inc in own.get("include", []) or []:
            merged.update({k: v for k, v in self.flat(inc, depth + 1).items() if k not in META})
        merged.update({k: v for k, v in own.items() if k not in ("inherits", "include")})
        return merged

    def filament_name(self, material):
        base = MATERIALS.get(material, MATERIALS["PLA"])[0]
        for candidate in (f"{base} @BBL X2D 0.4 nozzle", f"{base} @BBL X2D"):
            if candidate in self.paths:
                return candidate
        raise ValueError(f"No X2D preset for {material}")


def settings_files(exe, out: Path, material="PLA", walls=2, infill=15):
    """Write complete machine, process and filament JSON files for the command line."""
    p = Presets(profiles_dir(exe))
    machine, process = p.flat(MACHINE), p.flat(PROCESS)
    filament = p.flat(p.filament_name(material))
    process["wall_loops"] = str(int(walls))
    process["sparse_infill_density"] = f"{int(infill)}%"
    for cfg, kind in ((machine, "machine"), (process, "process"), (filament, "filament")):
        cfg["from"] = "system"
        cfg["instantiation"] = "true"
        cfg.setdefault("type", kind)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for kind, cfg in (("machine", machine), ("process", process), ("filament", filament)):
        files[kind] = out / f"{kind}.json"
        files[kind].write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return files


def read_estimate(path: Path):
    """Print time (s) and filament (g) from a sliced 3MF."""
    with zipfile.ZipFile(path) as z:
        info = z.read("Metadata/slice_info.config").decode("utf-8", "replace") if "Metadata/slice_info.config" in z.namelist() else ""
    seconds = sum(int(float(m)) for m in re.findall(r'key="prediction"\s+value="([\d.]+)"', info))
    grams = sum(float(m) for m in re.findall(r'key="weight"\s+value="([\d.]+)"', info))
    plates = len(re.findall(r"<plate>", info))
    return {"seconds": seconds, "grams": round(grams, 1), "plates": plates}


def slice_pieces(exe, pieces, out: Path, material="PLA", walls=2, infill=15, timeout=900):
    files = settings_files(exe, out / "settings", material, walls, infill)
    target = out / "print.gcode.3mf"
    target.unlink(missing_ok=True)
    cmd = [exe, "--arrange", "1", "--load-settings", f"{files['machine']};{files['process']}", "--load-filaments", str(files["filament"]),
           "--slice", "0", "--debug", "2", "--outputdir", str(out), "--export-3mf", target.name, *map(str, pieces)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(out),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (out / "slice.log").write_text(" ".join(f'"{c}"' for c in cmd) + f"\n\nexit {r.returncode}\n" + r.stdout[-20000:] + "\n" + r.stderr[-20000:], encoding="utf-8")
    if not target.is_file():
        detail = (r.stderr or r.stdout).strip()[-600:] or f"exit code {r.returncode}"
        raise ValueError("Bambu Studio couldn't slice it: " + detail)
    return target, read_estimate(target)
