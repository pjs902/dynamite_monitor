#!/usr/bin/env python3
"""
dynamite_monitor.py
====================
Monitor CPU and RAM of all Python/Fortran processes owned by a given user.
Writes per-sample JSONL and auto-stops when no Python processes remain.
Optionally tails a dynamite log file to show live model pipeline state.

Usage:
    python dynamite_monitor.py [options]

Options:
    --user USER              Unix username to monitor (default: pesmith)
    --interval SECONDS       Sample interval (default: 30 s)
    --output FILE            Output JSONL file (default: dynamite_monitor.jsonl)
    --duration SECONDS       Hard stop after N seconds (default: none)
    --fortran-patterns STR   Comma-separated Fortran binary substrings
                             (default: orb,triax,start,cmd_)
    --grace-periods N        Python-free polls before autostop (default: 5)
    --log FILE               Dynamite log file to tail for model state
                             (default: dynamite.log in cwd; use 'none' to disable)
    --no-live                Disable rich live display

Requirements:
    pip install psutil rich
    (rich is optional)

CPU% note:
    psutil reports cpu_percent as (cpu_time_delta / wall_time_delta) * 100,
    so one core pinned = 100%, all 192 cores = 19200%.
"""

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    sys.exit("psutil is required:  pip install psutil")

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# ── process classification ────────────────────────────────────────────────────

def _is_vscode(proc: psutil.Process) -> bool:
    try:
        cmdline = " ".join(proc.cmdline()).lower()
        exe     = (proc.exe() or "").lower()
        return ("vscode" in cmdline or "vscode" in exe or
                ".vscode-server" in cmdline or ".vscode-server" in exe)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def is_python_proc(proc: psutil.Process, user: str) -> bool:
    try:
        if proc.username() != user:
            return False
        if _is_vscode(proc):
            return False
        name = proc.name().lower()
        exe  = (proc.exe() or "").lower()
        return "python" in name or "python" in exe
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def is_target_process(proc: psutil.Process, user: str,
                       fortran_patterns: list) -> bool:
    try:
        if proc.username() != user:
            return False
        if _is_vscode(proc):
            return False
        name    = proc.name().lower()
        exe     = (proc.exe() or "").lower()
        cmdline = " ".join(proc.cmdline()).lower()
        if "python" in name or "python" in exe:
            return True
        return any(p in name or p in exe or p in cmdline
                   for p in fortran_patterns)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


# ── process cache (fixes cpu_percent always-0 on new Process objects) ─────────

class ProcessCache:
    def __init__(self):
        self._cache: dict[int, psutil.Process] = {}

    def prime(self, user: str, fortran_patterns: list) -> None:
        for proc in psutil.process_iter(["pid", "username"]):
            try:
                if proc.username() != user:
                    continue
                p = psutil.Process(proc.pid)
                p.cpu_percent()
                self._cache[proc.pid] = p
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                pass

    def snapshot(self, user: str, fortran_patterns: list) -> dict:
        ts = datetime.now(timezone.utc).isoformat()
        procs      = []
        total_cpu  = 0.0
        total_rss  = 0
        has_python = False
        seen_pids: set[int] = set()

        for proc in psutil.process_iter(["pid", "username"]):
            try:
                pid        = proc.pid
                user_match = (proc.username() == user)
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
            if not user_match:
                continue

            if pid not in self._cache:
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent()
                    self._cache[pid] = p
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            seen_pids.add(pid)
            p = self._cache[pid]

            if is_python_proc(p, user):
                has_python = True
            if not is_target_process(p, user, fortran_patterns):
                continue

            try:
                mem    = p.memory_info()
                rss    = mem.rss
                vms    = mem.vms
                cpu    = p.cpu_percent()
                cmd    = " ".join(p.cmdline())[:300]
                total_cpu += cpu
                total_rss += rss
                procs.append({
                    "pid":     pid,
                    "name":    p.name(),
                    "cmd":     cmd,
                    "status":  p.status(),
                    "cpu_pct": round(cpu, 2),
                    "rss_gb":  round(rss / 1024**3, 4),
                    "vms_gb":  round(vms / 1024**3, 4),
                    "threads": p.num_threads(),
                    "created": datetime.fromtimestamp(
                                   p.create_time(), tz=timezone.utc
                               ).isoformat(),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue

        dead = set(self._cache) - seen_pids
        for pid in dead:
            del self._cache[pid]

        vm      = psutil.virtual_memory()
        cpu_sys = psutil.cpu_percent(percpu=False)

        return {
            "ts":               ts,
            "has_python":       has_python,
            "n_procs":          len(procs),
            "total_cpu_pct":    round(total_cpu, 2),
            "total_rss_gb":     round(total_rss / 1024**3, 4),
            "sys_cpu_pct":      round(cpu_sys, 2),
            "sys_ram_used_gb":  round(vm.used      / 1024**3, 2),
            "sys_ram_avail_gb": round(vm.available / 1024**3, 2),
            "sys_ram_total_gb": round(vm.total     / 1024**3, 2),
            "sys_ram_pct":      round(vm.percent, 2),
            "processes":        procs,
        }


# ── log watcher ───────────────────────────────────────────────────────────────
#
# Tails the dynamite log file incrementally (tracks byte offset so we only
# ever read new lines).  Maintains a per-model state dict so the display can
# show what stage each model is currently in and how many are done.
#
# Model directory keys look like "orblib_001_002/ml03.00" — we extract these
# from the log messages and use them as stable identifiers.

# Stage labels in display order (most → least verbose)
STAGE_LABEL = {
    "queued":    ("queued",    "dim"),
    "orb_ics":   ("calc ICs", "cyan"),
    "orb_tube":  ("tube orbs","green"),
    "orb_box":   ("box orbs", "green"),
    "orb_par":   ("integrating","green"),
    "nnls":      ("NNLS",     "magenta"),
    "done":      ("done",     "bold green"),
    "failed":    ("FAILED",   "bold red"),
}

# Patterns: (regex, stage_key, done=False)
# Applied to each new INFO/WARNING/ERROR log line in order.
_LOG_PATTERNS = [
    # model announced
    (re.compile(r"running get_orblib get_weights for model \d+ out of \d+: (.+)/\s*$"),
     "queued", False),
    # orbit ICs start
    (re.compile(r"Calculating initial conditions for (.+?)\s*[./]?\s*$"),
     "orb_ics", False),
    # orbit ICs done  — path in previous context; match by orblib dir in datfil path
    (re.compile(r"cmd_orb_start exit code 0.*?Logfile: (.+?)/datfil/"),
     "orb_ics_done", False),   # handled specially below
    # tube orbits
    (re.compile(r"Integrating orbit library tube(?: and box)? orbits for (.+?)\s*[./]?\s*$"),
     "orb_tube", False),
    (re.compile(r"Integrating orbit library tube and box orbits for (.+?)\s*[./]?\s*$"),
     "orb_par", False),
    # box orbits
    (re.compile(r"Integrating orbit library box orbits for (.+?)\s*[./]?\s*$"),
     "orb_box", False),
    # NNLS start
    (re.compile(r"Using WeightSolver: NNLS.*?model (.+?)\s*[./]?\s*$"),
     "nnls", False),
    # NNLS done — model path in message
    (re.compile(r"NNLS problem solved and chi2 calculated for model (.+?)\s*[./]?\s*$"),
     "done", True),
    (re.compile(r"NNLS solution read from existing output (.+?)\s*[./]?\s*$"),
     "done", True),
]

# Simpler patterns whose path comes from a preceding "running…" line
_SIMPLE_STAGE = [
    (re.compile(r"Calculating initial conditions"),           "orb_ics"),
    (re.compile(r"Integrating orbit library tube and box"),   "orb_par"),
    (re.compile(r"Integrating orbit library tube orbits"),    "orb_tube"),
    (re.compile(r"Integrating orbit library box orbits"),     "orb_box"),
    (re.compile(r"Using WeightSolver"),                       "nnls"),
    (re.compile(r"NNLS problem solved"),                      "done"),
    (re.compile(r"NNLS solution read from existing"),         "done"),
    (re.compile(r"cmd_orb_start exit code 0"),                "orb_tube"),  # ICs finished → moving to integration
]

def _norm_model(raw: str) -> str:
    """Normalise a model path to 'orblib_XXX_YYY/mlZZ.ZZ' key."""
    raw = raw.strip().rstrip("/")
    # Keep only the last two path components that look like orblib/ml dirs
    parts = raw.replace("\\", "/").split("/")
    # Find orblib component
    orblib = next((p for p in reversed(parts) if p.startswith("orblib_")), None)
    ml     = next((p for p in reversed(parts) if p.startswith("ml")), None)
    if orblib and ml:
        return f"{orblib}/{ml}"
    if orblib:
        return orblib
    return parts[-1] if parts else raw


class LogWatcher:
    """
    Incrementally tails a dynamite log file.
    Maintains:
      self.models  : dict[model_key -> {stage, ts, label}]
      self.n_done  : int  — count of models that reached 'done'
      self.n_total : int  — total models announced so far
    """

    def __init__(self, logfile: str):
        self.logfile   = logfile
        self._offset   = 0
        self.models: dict[str, dict] = {}   # key → {stage, ts, label}
        self.n_done    = 0
        self.n_total   = 0
        self._last_model: str | None = None  # most recently announced model

        # Do an initial catch-up read if the file already exists
        if os.path.exists(logfile):
            self._read_new_lines()

    def poll(self) -> None:
        """Read any new lines appended since last poll. Very cheap."""
        if not os.path.exists(self.logfile):
            return
        self._read_new_lines()

    def _read_new_lines(self) -> None:
        try:
            with open(self.logfile, "r", errors="replace") as fh:
                fh.seek(self._offset)
                for raw_line in fh:
                    self._process_line(raw_line)
                self._offset = fh.tell()
        except OSError:
            pass

    # Log line format:
    #   [LEVEL] Mon DD HH:MM:SS - module - file:fn:line - message
    _LINE_RE = re.compile(
        r"\[(INFO|WARNING|ERROR|CRITICAL)\]\s+\w+ \d+ [\d:]+ - .+? - .+? - (.+)"
    )

    def _process_line(self, line: str) -> None:
        m = self._LINE_RE.match(line.strip())
        if not m:
            return
        level, msg = m.group(1), m.group(2).strip()

        # ── model announced ────────────────────────────────────────────────
        ann = re.search(
            r"running get_orblib get_weights for model \d+ out of \d+: (.+)", msg
        )
        if ann:
            key = _norm_model(ann.group(1))
            if key not in self.models:
                self.models[key] = {"stage": "queued", "ts": _now_str()}
                self.n_total += 1
            self._last_model = key
            return

        # ── path-bearing messages ──────────────────────────────────────────
        # Some messages contain a full ml-level path (orblib_XXX/mlYY.YY).
        # Others only contain the orblib-level path (orblib_XXX) because the
        # ICs and orbit integration steps work on the shared orblib directory,
        # not per-ml subdirectories.  In the latter case we advance ALL
        # announced models that share that orblib prefix.
        path_patterns = [
            (re.compile(r"Calculating initial conditions for (.+)"),                 "orb_ics"),
            (re.compile(r"Integrating orbit library tube and box orbits for (.+)"),  "orb_par"),
            (re.compile(r"Integrating orbit library tube orbits for (.+)"),          "orb_tube"),
            (re.compile(r"Integrating orbit library box orbits for (.+)"),           "orb_box"),
            (re.compile(r"NNLS problem solved and chi2 calculated for model (.+)"),  "done"),
            (re.compile(r"NNLS solution read from existing output (.+)"),            "done"),
        ]
        for pat, stage in path_patterns:
            pm = pat.search(msg)
            if pm:
                raw  = pm.group(1)
                key  = _norm_model(raw)
                # If key contains a '/' it's a full ml-level path — update directly
                if "/" in key:
                    self._set_stage(key, stage)
                else:
                    # orblib-only path: advance all models under this orblib dir
                    targets = [k for k in self.models if k.startswith(key + "/")]
                    if targets:
                        for t in targets:
                            self._set_stage(t, stage)
                    else:
                        # No announced models yet for this orblib; register placeholder
                        self._set_stage(key, stage)
                return

        # ── path-less messages ────────────────────────────────────────────
        # cmd_* exit messages don't include the path.  Advance models that are
        # in the logically preceding stage (handles parallel runs correctly by
        # advancing all candidates rather than guessing which one).
        pathless = [
            (re.compile(r"cmd_orb_start exit code 0"),  "orb_tube"),
            (re.compile(r"cmd_tube_orbs exit code 0"),  "orb_box"),
            (re.compile(r"cmd_box_orbs exit code 0"),   "nnls"),
            (re.compile(r"Using WeightSolver"),         "nnls"),
        ]
        preceding = {
            "orb_tube": "orb_ics",
            "orb_box":  "orb_tube",
            "nnls":     "orb_box",
        }
        for pat, stage in pathless:
            if pat.search(msg):
                expected_prev = preceding.get(stage)
                candidates = [
                    k for k, v in self.models.items()
                    if v["stage"] == expected_prev
                ] if expected_prev else []
                targets = candidates if candidates else (
                    [self._last_model] if self._last_model else []
                )
                for k in targets:
                    self._set_stage(k, stage)
                return

    def _set_stage(self, key: str, stage: str) -> None:
        if key not in self.models:
            self.models[key] = {"stage": "queued", "ts": _now_str()}
            self.n_total += 1
        prev = self.models[key]["stage"]
        self.models[key]["stage"] = stage
        self.models[key]["ts"]    = _now_str()
        # Count completions
        if stage == "done" and prev != "done":
            self.n_done += 1

    def summary(self) -> dict:
        """Return a serialisable snapshot of current model state."""
        return {
            "n_total":  self.n_total,
            "n_done":   self.n_done,
            "n_active": sum(1 for v in self.models.values() if v["stage"] != "done"),
            "models":   {k: dict(v) for k, v in self.models.items()},
        }


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── rich display ──────────────────────────────────────────────────────────────

N_CPUS = psutil.cpu_count(logical=True) or 1


def _bar(frac: float, width: int = 12) -> str:
    filled = int(round(max(0.0, min(1.0, frac)) * width))
    return "█" * filled + "░" * (width - filled)


def build_rich_table(rec: dict, n_samples: int,
                     peak_rss: float, peak_cpu: float,
                     elapsed_s: float, grace_left: int,
                     log_summary: dict | None) -> "Table":

    ts_str = rec["ts"][11:19] + " UTC"
    root   = Table.grid(padding=(0, 2))
    root.add_column()

    # ── summary header ────────────────────────────────────────────────────────
    hdr = Table(box=box.SIMPLE_HEAD, show_header=False,
                pad_edge=False, expand=True)
    hdr.add_column("k",  style="dim")
    hdr.add_column("v",  style="bold cyan")
    hdr.add_column("k2", style="dim")
    hdr.add_column("v2", style="bold magenta")

    ram_bar  = _bar(rec["sys_ram_used_gb"] / max(rec["sys_ram_total_gb"], 1))
    cpu_frac = rec["total_cpu_pct"] / (N_CPUS * 100.0)
    cpu_bar  = _bar(cpu_frac)

    hdr.add_row("time",       ts_str,        "samples",  str(n_samples))
    hdr.add_row("user procs", str(rec["n_procs"]),
                "elapsed",    f"{elapsed_s / 60:.1f} min")
    hdr.add_row("user cpu",
                f"{rec['total_cpu_pct']:.1f}%  {cpu_bar}  "
                f"({rec['total_cpu_pct'] / 100:.1f} cores)",
                "peak cpu",   f"{peak_cpu:.1f}%  ({peak_cpu/100:.1f} cores)")
    hdr.add_row("user rss",   f"{rec['total_rss_gb']:.3f} GB",
                "peak rss",   f"{peak_rss:.3f} GB")
    hdr.add_row("sys cpu",    f"{rec['sys_cpu_pct']:.1f}%",
                "sys ram",
                f"{rec['sys_ram_used_gb']:.0f}/{rec['sys_ram_total_gb']:.0f} GB "
                f"{ram_bar} {rec['sys_ram_pct']:.0f}%")

    # Model progress summary line (from log watcher)
    if log_summary and log_summary["n_total"] > 0:
        n_done   = log_summary["n_done"]
        n_total  = log_summary["n_total"]
        n_active = log_summary["n_active"]
        done_bar = _bar(n_done / n_total, width=20)
        hdr.add_row(
            "models",
            f"[bold green]{n_done}[/bold green][dim]/{n_total}[/dim] done  "
            f"{done_bar}  [cyan]{n_active} active[/cyan]",
            "", ""
        )

    if not rec["has_python"]:
        hdr.add_row(
            "[bold red]autostop[/bold red]",
            f"[bold red]no python procs — stopping after "
            f"{grace_left} more poll(s)[/bold red]",
            "", ""
        )

    root.add_row(hdr)

    # ── model state table (from log watcher) ──────────────────────────────────
    if log_summary and log_summary["models"]:
        mtbl = Table(box=box.SIMPLE, show_header=True,
                     header_style="bold dim", pad_edge=False, expand=True)
        mtbl.add_column("model",  width=36, no_wrap=True)
        mtbl.add_column("stage",  width=14)
        mtbl.add_column("since",  width=10, style="dim")

        # Show active models first, then done (most recent first within each)
        active  = [(k, v) for k, v in log_summary["models"].items()
                   if v["stage"] != "done"]
        done    = [(k, v) for k, v in log_summary["models"].items()
                   if v["stage"] == "done"]

        # Cap done rows to keep the table from growing huge over many models
        MAX_DONE = 5
        rows = active + done[:MAX_DONE]
        if len(done) > MAX_DONE:
            rows.append((f"... {len(done) - MAX_DONE} more done", {"stage": "done", "ts": ""}))

        for key, info in rows:
            stage = info["stage"]
            lbl, style = STAGE_LABEL.get(stage, (stage, "white"))
            mtbl.add_row(
                key,
                Text(lbl, style=style),
                info.get("ts", ""),
            )

        root.add_row(mtbl)

    # ── per-process table ─────────────────────────────────────────────────────
    ptbl = Table(box=box.SIMPLE, show_header=True,
                 header_style="bold dim", pad_edge=False, expand=True)
    ptbl.add_column("pid",    style="dim",     width=8)
    ptbl.add_column("name",   style="cyan",    width=20, no_wrap=True)
    ptbl.add_column("status", style="dim",     width=9)
    ptbl.add_column("cpu %",  justify="right", width=9)
    ptbl.add_column("cores",  justify="right", width=7)
    ptbl.add_column("rss GB", justify="right", width=9)
    ptbl.add_column("vms GB", justify="right", width=9)
    ptbl.add_column("thr",    justify="right", width=5)
    ptbl.add_column("command",                 no_wrap=True)

    for p in sorted(rec["processes"], key=lambda p: p["cpu_pct"], reverse=True):
        cores     = p["cpu_pct"] / 100.0
        cpu_style = "bold red" if cores >= 8   else "yellow" if cores >= 1.0 else "white"
        ram_style = "bold red" if p["rss_gb"] > 100 else "yellow" if p["rss_gb"] > 20 else "white"
        parts     = p["cmd"].split()
        short_cmd = (parts[0].split("/")[-1] + " " + " ".join(parts[1:3]))[:70] if parts else ""
        ptbl.add_row(
            str(p["pid"]), p["name"], p["status"],
            Text(f"{p['cpu_pct']:.1f}", style=cpu_style),
            Text(f"{cores:.1f}",        style=cpu_style),
            Text(f"{p['rss_gb']:.4f}",  style=ram_style),
            f"{p['vms_gb']:.4f}",
            str(p["threads"]),
            short_cmd,
        )

    if not rec["processes"]:
        ptbl.add_row("—", "—", "—", "—", "—", "—", "—", "—",
                     "[dim]no matching processes[/dim]")

    root.add_row(ptbl)
    return root


# ── plain-text fallback ───────────────────────────────────────────────────────

def plain_print(rec: dict, n: int, log_summary: dict | None) -> None:
    ts = rec["ts"][11:19]
    print(
        f"\r[{ts}] procs={rec['n_procs']:3d}  "
        f"cpu={rec['total_cpu_pct']:8.1f}% ({rec['total_cpu_pct']/100:.1f} cores)  "
        f"rss={rec['total_rss_gb']:8.4f} GB  "
        f"sys={rec['sys_ram_used_gb']:.0f}/{rec['sys_ram_total_gb']:.0f} GB "
        f"({rec['sys_ram_pct']:.0f}%)  n={n}",
        end="", flush=True
    )
    if log_summary and log_summary["n_total"] > 0:
        print(f"\n  models: {log_summary['n_done']}/{log_summary['n_total']} done  "
              f"{log_summary['n_active']} active")
        for k, v in log_summary["models"].items():
            if v["stage"] != "done":
                lbl, _ = STAGE_LABEL.get(v["stage"], (v["stage"], ""))
                print(f"    {k:<36}  {lbl}")
    elif 0 < rec["n_procs"] <= 30:
        print()
        for p in sorted(rec["processes"], key=lambda x: x["cpu_pct"], reverse=True):
            print(f"  pid={p['pid']:<8} {p['name']:<22} "
                  f"cpu={p['cpu_pct']:8.1f}% ({p['cpu_pct']/100:.1f} cores)  "
                  f"rss={p['rss_gb']:.4f} GB")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor dynamite process resource usage",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--user",             default="pesmith")
    parser.add_argument("--interval",         type=float, default=30.0,
                        help="Sample interval in seconds")
    parser.add_argument("--output",           default="dynamite_monitor.jsonl")
    parser.add_argument("--duration",         type=float, default=None,
                        help="Hard stop after N seconds")
    parser.add_argument("--fortran-patterns", default="orb,triax,start,cmd_",
                        help="Comma-separated Fortran binary substrings")
    parser.add_argument("--grace-periods",    type=int, default=5,
                        help="Python-free polls before autostop")
    parser.add_argument("--log",              default="dynamite.log",
                        help="Dynamite log file to tail (use 'none' to disable)")
    parser.add_argument("--no-live",          action="store_true",
                        help="Disable rich live display")
    args = parser.parse_args()

    use_rich         = HAS_RICH and not args.no_live and sys.stdout.isatty()
    fortran_patterns = [p.strip() for p in args.fortran_patterns.split(",") if p.strip()]
    logfile          = None if args.log.lower() == "none" else args.log

    print(f"dynamite_monitor  user={args.user!r}  interval={args.interval}s  "
          f"output={args.output}")
    print(f"logical cpus     : {N_CPUS}  (100% = 1 core, {N_CPUS*100}% = all cores)")
    print(f"fortran patterns : {fortran_patterns}")
    print(f"autostop         : after {args.grace_periods} consecutive "
          f"python-free polls ({args.grace_periods * args.interval:.0f} s)")
    if logfile:
        print(f"log watcher      : {logfile}"
              + (" (exists)" if os.path.exists(logfile) else " (waiting for file)"))
    else:
        print("log watcher      : disabled")
    if not HAS_RICH:
        print("note             : install 'rich' for live table  (pip install rich)")
    elif not use_rich and not args.no_live:
        print("note             : not a tty — live display disabled")
    print()

    # Initialise log watcher (does a catch-up read if file already exists)
    watcher = LogWatcher(logfile) if logfile else None

    # Prime cpu_percent counters
    cache = ProcessCache()
    print("priming cpu counters... ", end="", flush=True)
    cache.prime(args.user, fortran_patterns)
    psutil.cpu_percent(percpu=False)
    print("done. waiting 5s before first sample.\n")
    time.sleep(5)

    _stop = False

    def _handler(sig, frame):
        nonlocal _stop
        _stop = True

    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)

    t_start          = time.monotonic()
    n_samples        = 0
    peak_rss         = 0.0
    peak_cpu         = 0.0
    no_python_streak = 0

    def hard_stop() -> bool:
        return _stop or bool(
            args.duration and (time.monotonic() - t_start) >= args.duration
        )

    with open(args.output, "a") as fh:

        def run_loop(live=None):
            nonlocal n_samples, peak_rss, peak_cpu, no_python_streak

            while not hard_stop():
                # Poll log watcher first (very cheap — just reads new bytes)
                if watcher:
                    watcher.poll()
                log_summary = watcher.summary() if watcher else None

                rec     = cache.snapshot(args.user, fortran_patterns)
                elapsed = time.monotonic() - t_start
                n_samples += 1

                peak_rss = max(peak_rss, rec["total_rss_gb"])
                peak_cpu = max(peak_cpu, rec["total_cpu_pct"])

                # Embed log summary in the JSONL record too
                if log_summary:
                    rec["log_summary"] = log_summary

                fh.write(json.dumps(rec) + "\n")
                fh.flush()

                if not rec["has_python"]:
                    no_python_streak += 1
                else:
                    no_python_streak = 0

                grace_left = max(0, args.grace_periods - no_python_streak)

                if live is not None:
                    live.update(build_rich_table(
                        rec, n_samples, peak_rss, peak_cpu,
                        elapsed, grace_left, log_summary,
                    ))
                else:
                    plain_print(rec, n_samples, log_summary)

                if no_python_streak >= args.grace_periods:
                    msg = (f"autostop: no Python processes for "
                           f"{args.grace_periods} consecutive polls.")
                    if live is not None:
                        Console().print(f"\n[bold green]{msg}[/bold green]")
                    else:
                        print(f"\n{msg}")
                    break

                time.sleep(args.interval)

        if use_rich:
            console  = Console()
            init_log = watcher.summary() if watcher else None
            init_rec = cache.snapshot(args.user, fortran_patterns)
            with Live(
                build_rich_table(init_rec, 0, 0.0, 0.0, 0.0,
                                 args.grace_periods, init_log),
                console=console,
                refresh_per_second=2,
                screen=False,
            ) as live:
                run_loop(live)
        else:
            run_loop(live=None)

    elapsed_total = time.monotonic() - t_start
    print(
        f"\n{n_samples} samples  |  {elapsed_total / 60:.1f} min  |  "
        f"peak rss={peak_rss:.3f} GB  |  peak cpu={peak_cpu:.1f}% "
        f"({peak_cpu/100:.1f} cores)  |  written to {args.output}"
    )
    if watcher:
        s = watcher.summary()
        print(f"models           : {s['n_done']}/{s['n_total']} done")


if __name__ == "__main__":
    main()
