# Implementation notes

Design decisions, non-obvious behaviours, and gotchas for anyone maintaining or extending this tooling.

---

## dynamite_monitor.py

### CPU measurement — the psutil first-call problem

`psutil.cpu_percent()` works by computing `(cpu_time_now − cpu_time_last_call) / wall_time_elapsed × 100`. The critical implication: it returns `0.0` on the **first call** for any given `Process` object, because there is no prior timestamp to diff against.

The naive approach — calling `psutil.process_iter()` with attribute caching on every poll — silently creates a new `Process` object for each PID on each call. Every poll therefore sees 0% CPU for everything, which is what the original implementation was doing.

**Fix: `ProcessCache`**

A `dict[int, psutil.Process]` persists `Process` objects across polls. On each poll we iterate `process_iter()` only to enumerate alive PIDs (cheap), then look up or create a cached `Process` object per PID, and call `cpu_percent()` on the *same object* as last time. This gives a genuine delta over the interval.

New PIDs that appear mid-run are primed on first encounter (returning 0.0 that one time, correctly) and evicted when their PID disappears.

At startup there is a 5-second sleep between priming and the first real snapshot. This is long enough for the kernel's `/proc/<pid>/stat` counters to accumulate a non-trivial delta, but short enough not to be annoying. Previously this was `time.sleep(args.interval)` (30 s), which was unnecessary — the accuracy of the first reading is not meaningful and not worth 30 s of delay.

### CPU% scale

`psutil` reports per-process CPU% as `(process_cpu_time_delta / wall_time_delta) × 100`. A process pinning one core = 100%. A Fortran orbit integration binary using all 192 cores = 19200%. This is the correct and expected representation; it is not an error.

The live display converts to "cores" (`cpu_pct / 100`) as a parallel label and scales the progress bar against `N_CPUS × 100` so it reflects actual cluster utilisation.

### VS Code exclusion

The VS Code remote server (`~/.vscode-server/`) runs a `node` process and multiple Python helper processes owned by the same user. Without exclusion these appear as dynamite activity: the Python processes inflate process counts and can trigger false autostop.

The exclusion checks both `cmdline` and `exe` for the strings `vscode` and `.vscode-server`. Checking both is necessary because some VS Code helper processes have a short `python` name but a path pointing into `.vscode-server`.

The exclusion is applied in two places: `is_python_proc()` (which drives the autostop logic) and `is_target_process()` (which drives what gets logged). Both must exclude VS Code independently to prevent phantom autostop triggers.

### Autostop grace periods

The autostop counter increments when `has_python` is `False` for an entire poll cycle. `has_python` is determined by scanning **all** user-owned processes, not just the filtered "target" ones — this means the autostop condition is: no Python interpreter at all owned by this user (excluding VS Code). The logic intentionally stops when dynamite's coordinator Python exits, even if Fortran binaries are still winding down.

The default grace period is 5 polls (150 s at 30 s interval). This bridges the gap between model iterations, where the coordinator briefly exits between spawning new Fortran jobs. If your cluster is slow to schedule jobs and the gap between iterations can be longer, increase `--grace-periods`.

### Process target matching

A process is a "target" if it is owned by the monitored user and either:
- its name or executable path contains `python`, or
- any string in `--fortran-patterns` appears in its name, exe path, or full cmdline

The cmdline check is necessary for some Fortran binaries that are launched via shell wrappers (`bash cmd_tube_box_orbs`), where the binary name appears in the argument string rather than the process name.

### Log watcher — incremental reads

`LogWatcher` stores the byte offset of the last read. Each `poll()` call opens the file, seeks to the stored offset, reads only new bytes, and updates the offset. For a 24-hour run producing perhaps 50 MB of debug log, each poll reads at most a few KB of new lines. The overhead is negligible compared to the `psutil.process_iter()` scan.

If the log file does not yet exist (e.g. the monitor is started before dynamite), `poll()` is a no-op until the file appears. This is intentional and safe.

### Log watcher — path granularity problem

DYNAMITE's log uses two different path granularities depending on the pipeline step:

- **ML-level paths** (`orblib_000_000/ml02.60/`): appear in model-announcement lines and in NNLS messages. These can be matched directly to a specific model.
- **Orblib-level paths** (`orblib_000_000/`): appear in orbit integration messages (ICs, tube, box). This is because a single set of orbital parameters (`orblib_XXX_YYY`) is shared across all mass-to-light values (`ml`), and the orbit library is computed once and reused. The log therefore refers to the orblib directory, not any individual `ml` subdirectory.

When the watcher sees an orblib-level path, it fans the stage update out to **all models whose key starts with that orblib prefix**. For example, if `orblib_001_000/ml02.60` and `orblib_001_000/ml03.00` have both been announced, a message about `orblib_001_000/` advances both simultaneously, which correctly reflects that they share the same orbit computation.

### Log watcher — path-less messages

Several log messages do not contain a path at all, e.g.:

```
...done - cmd_tube_orbs exit code 0. Logfiles: ...
Using WeightSolver: NNLS
```

These are matched by transitioning models from their logically preceding stage:
- `cmd_orb_start exit code 0` → advances any model in `orb_ics` to `orb_tube`
- `cmd_tube_orbs exit code 0` → advances any model in `orb_tube` to `orb_box`
- `cmd_box_orbs exit code 0` → advances any model in `orb_box` to `nnls`
- `Using WeightSolver` → marks any model in `orb_box` as entering `nnls`

When multiple models are in the same stage (parallel orblib computation), all candidates are advanced together. This is correct because the log does not distinguish which orblib's computation just finished in these path-less lines.

### Model progress — "announced so far" not "total"

DYNAMITE's `GridWalk` and `LegacyGridSearch` parameter generators announce models iteration by iteration. Each iteration's models are announced via `running get_orblib get_weights for model N out of M` lines, where `M` is the count for **that iteration only**. Future iterations are not announced in advance.

The watcher counts `n_total` as the number of models that have been announced (i.e. seen in the log). This grows monotonically throughout the run. The viewer's progress bar shows `n_done / n_total` with an explicit label "of announced models — more iterations may follow" to avoid implying a known endpoint.

---

## dynamite_viewer.html

### Single-file architecture

The viewer is entirely self-contained: HTML, CSS, and JavaScript in one file, with a single external CDN dependency (Chart.js from cdnjs.cloudflare.com). This means it can be committed to a repo, emailed, or opened directly from a cluster filesystem mount without any build step or server.

The only meaningful constraint this imposes is that all state is in-memory. There is no local storage or IndexedDB usage — if you reload the page, you re-drop the files.

### JSONL parsing

Each line of the monitor output is parsed with `JSON.parse()` independently. Malformed lines (from an interrupted write) are silently dropped with a `try/catch`. This means partial JSONL files from interrupted runs are still usable.

The `log_summary` field is optional and absent from records written before the log watcher sees any model announcements. `renderModelStatus()` scans backwards from the last record to find the most recent one with `log_summary` present.

### Log timestamp parsing

Dynamite's log format is:

```
[LEVEL] Mon DD HH:MM:SS - module - file:fn:line - message
```

The timestamp contains no year. The viewer assumes the current year, which is correct for live monitoring but will be wrong for logs spanning a year boundary (unlikely for a single modelling run). The parsed `Date` object is used only for computing fractions within the monitor time window, so a one-year error would simply place events outside `[0, 1]` and they would be filtered out by the `frac >= -0.02 && frac <= 1.02` guard.

### Stage classifier

Log lines are matched against `STAGE_RULES` in order; the first matching rule wins. Rules with `_levelOnly` are skipped if the line's level does not match (used to catch all WARNING/ERROR/CRITICAL lines as a catch-all without misclassifying INFO lines with similar text).

The `extract` function on a rule receives the regex match object and returns the human-readable detail string shown in tooltips and the log table. If absent, the first 100 characters of the message are used.

### Annotation lines on charts

The Chart.js annotation plugin is implemented as a custom `afterDraw` plugin rather than using the `chartjs-plugin-annotation` library. This avoids an additional CDN dependency and gives direct control over rendering.

Nearby annotations are bucketed (1 bucket per 0.5% of the time axis) to avoid drawing hundreds of overlapping lines when many events occur close together. Only one line is drawn per bucket, but all events in that vicinity are shown in the tooltip.

### Chart hover accuracy

The annotation lines are positioned using `frac × (ca.right - ca.left) + ca.left`, where `ca` is `chart.chartArea`. The hover detection uses the same calculation in reverse. This is important because Chart.js allocates approximately 50–70 px on the left for y-axis tick labels and a small margin on the right — using raw canvas pixel fraction without accounting for the chart area would place annotation hits significantly to the left of their visual position.

The chart instance is passed as a lambda (`() => cpuChart`) rather than directly, because the chart object is reassigned on each `renderCharts()` call (old chart is destroyed, new one created). The lambda captures the variable name, not the object reference, so it always returns the current chart.

### Phase strip canvas

The strip is a `<canvas>` element sized to its container width via `canvas.offsetWidth`. Each event is rendered as:
- A 1 px wide vertical line spanning the full strip height, at 80% opacity
- A 3×3 px "diamond cap" at the top at full opacity

The cap makes individual ticks visible even when many lines are very close together — the slightly different opacity between cap and line creates a visual hierarchy. No fill is drawn between events because multiple models can be active simultaneously and a filled region would falsely imply a single active stage.

Hover detection on the strip uses an 8 px snap radius and collects all events within that window, showing them stacked in the tooltip with dividers between entries.

### Model status panel ordering

Active models are sorted by pipeline stage in forward execution order (ICs → tube → box → NNLS → queued), so the model closest to completion appears at the top. Done models are appended after, limited to 5 rows with a "N more completed" footnote to prevent the table growing unboundedly over a long multi-iteration run.

### Colour system

The viewer uses a light theme (`#f4f4f0` background, white cards) with high-contrast primary text (`#1a1a1a`). Stage colours are chosen from a palette of saturated accessible colours against white: blue (`#2563eb`) for CPU, violet (`#7c3aed`) for RAM, amber (`#d97706`) for process count. Stage pill backgrounds use low-saturation tints (e.g. `#dbeafe` for orbit ICs) with a dark text from the same hue family (`#1d4ed8`) to maintain WCAG AA contrast.

---

## Known limitations and future work

**Log watcher path matching is heuristic.** The `_norm_model()` function extracts `orblib_XXX_YYY/mlZZ.ZZ` from arbitrary path strings. It looks for the last component starting with `orblib_` and the last starting with `ml`. If DYNAMITE changes its directory naming convention, this will break silently — models will be registered with unexpected keys and stage tracking will diverge from reality.

**Year boundary in log timestamps.** As noted above, the viewer's log timestamp parser assumes the current year. A run crossing 31 December → 1 January would have the first few hours of the new year misattributed to the previous year, causing those events to appear outside the monitor time window and not be plotted.

**Parallel orblib computation path-less messages.** When `orblibs_in_parallel: True` (as in the NGC5139 config), multiple orblists may finish at similar times. The `cmd_tube_orbs exit code 0` message does not contain a path, and the watcher advances all models in the preceding stage. If models in the same stage finish at different times, they are all advanced on the first completion message, which may be slightly early for some. This is cosmetic — it does not affect the `n_done` count, which is driven by the path-bearing NNLS completion messages.

**No websocket / live reload.** The viewer is a post-run analysis tool, not a live dashboard. To watch a run in progress, either reload the page and re-drop the JSONL file, or use the terminal monitor directly. A future version could add a file polling mechanism using the File System Access API, but this requires browser permissions and is not universally supported.

**Python 3.10+ required.** `dynamite_monitor.py` uses `dict | None` union type hints in function signatures, which requires Python 3.10. If your environment is older, replace `dict | None` with `Optional[dict]` and add `from typing import Optional`.
