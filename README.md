# dynamite-monitor

Resource monitoring and visualisation tools for long-running [DYNAMITE](https://github.com/dynamics-of-stellar-systems/dynamite) Schwarzschild modelling runs.

Two files, no dependencies beyond what a dynamite environment already has:

| File | Purpose |
|---|---|
| `dynamite_monitor.py` | Runs on the cluster alongside dynamite. Records CPU/RAM per process and tracks pipeline stage from the log file. |
| `dynamite_viewer.html` | Standalone browser app. Drop in the `.jsonl` output and optionally the dynamite log to get annotated timeseries charts and a model status table. |

---

## Quick start

### On the cluster

```bash
pip install psutil rich   # rich is optional but recommended
```

Start the monitor in a `tmux` or `screen` session before launching dynamite:

```bash
# in one pane
python dynamite_monitor.py --user pesmith --output run_001.jsonl

# in another pane
python my_dynamite_script.py
```

The monitor will auto-stop 150 s after the last Python process owned by your user exits (5 polls × 30 s default interval).

### In the browser

Open `dynamite_viewer.html` locally — no server required, everything runs in the page.

1. Drop `run_001.jsonl` onto the left zone → resource charts appear
2. Drop `dynamite.log` onto the right zone → pipeline stage annotations and model status table appear

---

## Monitor options

```
--user              Unix username to watch          (default: pesmith)
--interval          Sample interval in seconds      (default: 30)
--output            Output JSONL file               (default: dynamite_monitor.jsonl)
--duration          Hard stop after N seconds       (default: none)
--fortran-patterns  Comma-separated substrings
                    matching Fortran binary names   (default: orb,triax,start,cmd_)
--grace-periods     Python-free polls before
                    autostop                        (default: 5 = 150 s)
--log               Dynamite log file to tail       (default: dynamite.log)
                    use --log none to disable
--no-live           Disable rich live display
```

### Choosing an interval

The default 30 s is calibrated for DYNAMITE runs where the fastest step (orbit initial conditions) takes around 5 minutes. This gives roughly 10 data points per pipeline step and around 2 900 samples over a 24-hour run.

For shorter exploratory runs or debugging you can use `--interval 10`. For very long multi-day runs `--interval 60` keeps the file small.

### Fortran binary detection

DYNAMITE delegates the heavy computation to Fortran binaries. The `--fortran-patterns` flag controls which process names are recognised. The defaults cover the standard legacy_fortran executables: `orblib_new_mirror` (tube orbits), `orblib_box_mirror` (box orbits), `cmd_orb_start` (initial conditions), and their variants. If you have a custom build with different names, extend the list:

```bash
python dynamite_monitor.py --fortran-patterns orb,triax,start,cmd_,my_custom_binary
```

### VS Code pollution

Any process whose command line or executable path contains `vscode` or `.vscode-server` is excluded automatically. This prevents the VS Code remote server and its many helper Python processes from appearing as dynamite activity and triggering false autostop.

---

## Live display

When running in a terminal (TTY) with `rich` installed, the monitor shows a live table that refreshes every poll:

```
dynamite_monitor  user='pesmith'  interval=30.0s  output=run_001.jsonl
logical cpus     : 192  (100% = 1 core, 19200% = all cores)
fortran patterns : ['orb', 'triax', 'start', 'cmd_']
autostop         : after 5 consecutive python-free polls (150 s)
log watcher      : dynamite.log (exists)

priming cpu counters... done. waiting 5s before first sample.

time         17:42:54 UTC          samples      7
user procs   17                    elapsed      3.1 min
user cpu     4850.1%  ████░░░░░░░░  (48.5 cores)   peak cpu  4850.1%
user rss     2.050 GB              peak rss     2.090 GB
sys cpu      67.9%                 sys ram      139/1417 GB █░░░░░░░░░░░  10%
models       1/3 done  ████████░░░░░░░░░░░░  2 active

model                                stage         since
orblib_000_000/ml02.60               NNLS          17:41:03
orblib_001_000/ml02.60               tube orbs     17:40:22
orblib_001_001/ml02.60               done          17:39:55

pid       name                 status    cpu %   cores   rss GB   vms GB  thr  command
1060494   orblib_new_mirror    running   9610.0   96.1   0.2031   0.2076    1  orblib_new_mirror
...
```

The CPU bar scales against the machine's total logical core count, so the bar represents actual cluster utilisation rather than being pinned at 100%.

Use `--no-live` to suppress the display when running non-interactively (e.g. via `nohup` or a job scheduler).

---

## JSONL output format

Each line is a JSON object:

```json
{
  "ts": "2025-04-15T12:17:54+00:00",
  "has_python": true,
  "n_procs": 17,
  "total_cpu_pct": 4850.1,
  "total_rss_gb": 2.050,
  "sys_cpu_pct": 67.9,
  "sys_ram_used_gb": 139.0,
  "sys_ram_avail_gb": 1278.0,
  "sys_ram_total_gb": 1417.0,
  "sys_ram_pct": 9.8,
  "processes": [
    {
      "pid": 1060494,
      "name": "orblib_new_mirror",
      "cmd": "orblib_new_mirror",
      "status": "running",
      "cpu_pct": 9610.0,
      "rss_gb": 0.2031,
      "vms_gb": 0.2076,
      "threads": 1,
      "created": "2025-04-15T12:10:03+00:00"
    }
  ],
  "log_summary": {
    "n_total": 3,
    "n_done": 1,
    "n_active": 2,
    "models": {
      "orblib_000_000/ml02.60": { "stage": "nnls",     "ts": "17:41:03" },
      "orblib_001_000/ml02.60": { "stage": "orb_tube", "ts": "17:40:22" },
      "orblib_001_001/ml02.60": { "stage": "done",     "ts": "17:39:55" }
    }
  }
}
```

`log_summary` is present only when the log watcher is active and has seen at least one model announcement. `cpu_pct` follows the psutil convention: 100% means one core fully utilised, so values above 100% are normal and expected for multi-threaded Fortran binaries.

---

## Viewer

`dynamite_viewer.html` is a single self-contained HTML file with no build step and no server requirement. Open it in any modern browser.

### What it shows

**Resource metrics** — peak RSS, peak CPU (user processes), peak system RAM, total samples and run duration.

**Model progress** (when JSONL contains `log_summary`) — count of done / active / announced models with a progress bar. The label explicitly says "of announced models" because DYNAMITE's GridWalk/LegacyGridSearch adds new iterations until chi² converges — the true total is not knowable until the run finishes.

**Model status table** — one row per model with a colour-coded stage pill (queued → calc ICs → tube orbits → box orbits → NNLS → done). Active models appear first, completed models are collapsed after 5 rows.

**Pipeline event timeline** — a strip of vertical tick marks, one per classified log event, colour-coded by stage. Hover any tick (or hover an annotation line on the charts) to see a tooltip with the full event detail, timestamp, and all co-occurring events if multiple models were active at the same time.

**CPU / RAM / process count charts** — timeseries with pipeline stage annotation lines. Hovering near an annotation line shows the tooltip. Charts are Chart.js with a 6 px snap radius for hover detection.

**Pipeline event log** — filterable table of all classified log events with stage badges. Stages are: config, parameter space, model start, orbit ICs, tube orbits, box orbits, tube+box orbits (parallel), NNLS weights, iteration done, plotting, warning/error.

**Process snapshot** — table of all monitored processes from the most recent JSONL sample, sortable by any column.

---

## Model progress — what "announced" means

DYNAMITE announces models one iteration at a time. In each iteration the log emits a line like:

```
running get_orblib get_weights for model 3 out of 5: NGC5139_output/models/orblib_001_000/ml02.60/
```

The watcher counts models as they are announced and tracks how many reach the `done` state. It never claims to know how many remain across future iterations, because:

- `GridWalk` adds models around the current chi² minimum at each iteration until convergence
- `LegacyGridSearch` halves step sizes across iterations until `minstep` is reached
- Neither algorithm announces future-iteration models in advance

The progress bar therefore shows "N done out of M announced so far", with a note that more iterations may follow.

---

## Requirements

```
psutil>=5.9     # process introspection
rich>=13        # live terminal display (optional)
```

Python 3.10+ (uses `dict | None` type hints).

The viewer requires only a modern browser — Chrome, Firefox, or Safari from 2022 onwards.
