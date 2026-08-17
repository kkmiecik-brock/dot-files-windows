# Tiling robustness tasks

Working list derived from `docs/tiling/windows-window-management-research.md`
(GlazeWM event-handling research). Ordered highest to lowest priority - work
through one at a time, mark done, keep this file up to date as the source of
truth for what's left.

This `docs/` folder is repo-only documentation - it is intentionally NOT
copied by `initialize.ps1` into the deployed `%USERPROFILE%\.config\oriel`
runtime copy. Keep it that way: don't move docs back under `src/`, and don't
add docs/task-list content to the deployed package.

## Status legend
- [ ] not started
- [~] in progress
- [x] done

---

## 1. [x] Move hardcoded per-application ignore rules into config-driven data

**Module:** `src/oriel/tiling/filters.py`, `src/oriel/tiling/events.py`, `config.json`

**Priority: FOUNDATIONAL (architecture, not a bug)**

**Status:** Implemented and verified (2026-08-13). Removed `IGNORE_CLASSES`,
`IGNORE_PROCESSES`, `IGNORE_TITLES`, and `_extra_ignore_rules` (which had
grown application-specific `if process_name == "firefox.exe" and ...`-style
branches baked directly into `is_manageable`'s code path). Replaced with a
generic rule matcher (`_rule_matches`/`_is_ignored`) that reads a list of
rules from `config.json`'s `tiling.ignore_rules` (loaded via
`filters.load_ignore_rules()`, wired into `events.apply_initial_settings()`
and `events.reload_settings()` the same way `inner_gap`/`outer_gap` already
were). Each rule is a dict of fields (`process`, `class`, `class_contains`,
`class_not`, `title`, `title_contains`; values may be a string or a list
meaning "any of") ANDed together, with rules ORed across the list. All 14
original exclusions were ported into `config.json` as data with byte-for-
byte equivalent matching behavior - verified via direct calls to
`filters._is_ignored(...)` reproducing every original case (Teams
notification, VS Code helper window, Firefox PiP, Office main-window-class
allowlisting, Task Manager, a normal unaffected app), then live-tested that
tiling still works end-to-end after reload. No application-specific code
remains in `filters.py` - adding a new exclusion is now a config.json edit,
not a code change. This directly addresses wanting a "sound architecture
and execution path" instead of special-casing baked into the code for every
small bug (e.g. the Firefox PiP fix from earlier this session, which is now
just a data entry rather than an `if` branch).

**Problem (historical):** `filters.py`'s structural checks (visibility,
ownership, style bits) were solid and generic, but the secondary exclusion
layer had accumulated real per-application code branches over time (Teams,
VS Code, msrdc/Windows 365, Firefox/Edge/Chrome dialogs, Firefox
Picture-in-Picture, Office apps, UWP Settings) - every new exclusion meant
editing Python source and redeploying, rather than being data the system
reads.

---

## 2. [x] Handle `EVENT_OBJECT_HIDE` / `EVENT_OBJECT_CLOAKED` / `EVENT_OBJECT_UNCLOAKED`

**Module:** `src/oriel/tiling/events.py`

**Priority: HIGH**

**Status:** Implemented and verified live (2026-08-13). Added
`on_window_hidden(hwnd)`, hooked `EVENT_OBJECT_HIDE`/`EVENT_OBJECT_CLOAKED`
to it, and added `EVENT_OBJECT_UNCLOAKED` alongside `EVENT_OBJECT_SHOW`/
`EVENT_OBJECT_NAMECHANGE` in `on_window_shown`'s dispatch. Guards against
stale/out-of-order notifications by rechecking `IsWindowVisible`/
`is_cloaked` before actually unmanaging (renamed `filters._is_cloaked` to
public `filters.is_cloaked` since events.py needed it too). Verified live:
hid one of two tiled Notepad windows via `ShowWindow(SW_HIDE)` - the other
window correctly expanded to fill the freed space; showing it again via
`SW_SHOW` correctly re-split the layout back to the original two widths.

**Problem:** oriel only removes a window from the tiling tree on
`EVENT_OBJECT_DESTROY`. A window that's hidden (`ShowWindow(SW_HIDE)`)
without being destroyed, or moved to another Windows virtual desktop
(DWM-cloaked), stays in the tree forever - a "ghost tile" that keeps
consuming layout space and getting redundant `SetWindowPos` calls in
`reflow()`. There is currently no cleanup path for this at all (the old
`prune_closed` polling safety net was removed in the 2026-08-12 session -
see repo memory notes - and no event-driven equivalent exists for
hide/cloak).

**Reference:** research doc sections "`EVENT_OBJECT_HIDE`",
"`EVENT_OBJECT_CLOAKED`", "`EVENT_OBJECT_UNCLOAKED`", and "Treat show/hide
and cloak/uncloak as paired visibility channels" under "Important design
lessons for Oriel".

**Plan:**
- Hook `EVENT_OBJECT_HIDE` (`0x8003`) and `EVENT_OBJECT_CLOAKED` (`0x8017`)
  in `events.py` / `_win_event_proc`, both routed to a new handler (e.g.
  `on_window_hidden(hwnd)`) that removes the leaf from the tree the same way
  `on_window_destroyed` does, but without treating the hwnd as gone.
- Hook `EVENT_OBJECT_UNCLOAKED` (`0x8018`) alongside `EVENT_OBJECT_SHOW` so
  a window returning from another virtual desktop gets re-managed the same
  way a newly shown window does.
- Verify against `IsWindowVisible`/DWM cloak state before acting, since
  GlazeWM's own notes warn about stale/out-of-order hide notifications
  ("unmanage it only after native visibility confirms it is actually
  hidden").

---

## 3. [x] Newly opened windows can end up on the wrong monitor with a blank tile left behind

**Module:** `src/oriel/tiling/events.py`

**Priority: HIGH (reported live bug, not speculative)**

**Status:** Fixed and verified (2026-08-13). Confirmed the hypothesis with a
controlled repro: tile a window via oriel, then programmatically
`SetWindowPos` it to a different monitor (simulating an app restoring its
own remembered position) - before the fix, the window visibly stuck on
the other monitor (real negative-x monitor-2 coordinates observed) until
some *unrelated* later reflow of the original monitor incidentally yanked
it back, leaving the tree/tile relationship stale in between (the reported
"blank tile" window). Fixed by adding a bounded settling window: whenever
`on_window_shown` tiles a window, it records `hwnd -> monotonic expiry` in
`_settling_hwnds` (`MONITOR_SETTLE_WINDOW = 2.0`s). Every subsequent
`EVENT_OBJECT_LOCATIONCHANGE`/`EVENT_SYSTEM_FOREGROUND` for that hwnd (via
the existing hook, no new hook needed) now also calls
`_reassert_monitor_if_settling(hwnd)`, which compares
`geometry.monitor_of(hwnd)` (the window's real, current monitor) against
the monitor its tree leaf is actually filed under, and calls
`_state.reflow(monitor, workspace)` to snap it straight back if they've
diverged - consistent with `on_window_shown`'s existing design intent that
cursor-driven placement wins over wherever an app decides to put itself.
Entries are cleaned up in `on_window_destroyed`/`on_window_hidden`, same as
the existing pending-manageable bookkeeping. Verified live, before and
after: pre-fix the simulated self-move stuck on the other monitor for the
full observation window; post-fix it never visibly leaves the tree's
monitor at all (reasserted before the next check). No timer backstop was
added - `SetWindowPos`/`MoveWindow` always generates
`EVENT_OBJECT_LOCATIONCHANGE`, so the event-driven check alone is
sufficient here (unlike the `is_manageable` retry case, which needed a
timer because a hwnd can otherwise go silent with no further events at
all).

**Problem (historical):** New windows are supposed to open on whichever
monitor the cursor is on - `on_window_shown` computes
`geometry.monitor_at_cursor()`, inserts the new leaf into that monitor's
tree, and reflows it there. Reported symptom: the window visually opens on
a different ("second") monitor, while the monitor the cursor was actually
on shows a blank tile slot where the tree expected the new window to be.

**Hypothesis** (consistent with this session's established pattern of apps
repositioning themselves asynchronously after their own SHOW event - see
the `is_manageable` timing race fixed via `recheck_if_pending`/the
manageable-retry mechanism in `events.py` (repo memory has the full
write-up), and `drag/daemon.py`'s own docstring about apps "fighting"
externally-imposed placement):
`on_window_shown` correctly inserts the window into the cursor's monitor
tree and `reflow()` does call `SetWindowPos` to move the real window there
- but sometime *after* that, the application itself performs its own async
`SetWindowPos`/`MoveWindow` (e.g. restoring a remembered last-used
monitor/position from its own settings - a very common app behavior),
silently overriding oriel's placement. Since this is a purely programmatic
move, it generates `EVENT_OBJECT_LOCATIONCHANGE`, not an interactive
move/resize event - and oriel's LOCATIONCHANGE handler
(`recheck_if_pending`) only reacts if the hwnd is still in
`_pending_manageable_hwnds`, which it no longer is once it's already been
tiled once. Net effect: the tree still holds a leaf for the window (hence
the blank tile slot - `reflow()` keeps reserving space for it, but the
window itself isn't there anymore), while the real window sits wherever the
app moved itself to, now completely unmanaged.

**Reference:** ties into the same "recheck state after later events"
principle as the manageable-retry mechanism and the research doc's
`EVENT_OBJECT_LOCATIONCHANGE` section, but applied to monitor placement
instead of just manageability.

**Plan:**
- Confirm the hypothesis empirically first (open an app known to remember
  its own window position/monitor - a browser, VS Code, etc. - on the
  non-cursor monitor once so it has a remembered position there, close it,
  then reopen it while the cursor is on a *different* monitor, and poll its
  rect over the following ~1-2 seconds the same way the Firefox
  `is_manageable` race was diagnosed, to see if/when it silently moves
  itself away from where oriel placed it).
- If confirmed: extend the LOCATIONCHANGE handling so a newly-inserted
  window's *actual* current monitor is re-checked for a short window after
  insertion (not just "is it manageable" but "is it still on the monitor
  the tree thinks it's on"). If the real window's monitor no longer matches
  its tree monitor, either move its leaf to the tree for the monitor it
  actually ended up on, or re-assert its correct position with another
  `SetWindowPos`.
- Consider bounding this the same way as the manageable-retry (a
  short-lived tracked set + timer backstop) - task 1's sibling fix already
  proved that an event-driven-only approach isn't always sufficient for
  this class of async app behavior, so don't assume LOCATIONCHANGE alone
  will be enough here either without verifying.

---

## 4. [ ] Handle monitor/display configuration changes

**Module:** `src/oriel/tiling/events.py`, `src/oriel/tiling/daemon.py`

**Priority: HIGH**

**Problem:** oriel never listens for `WM_DISPLAYCHANGE` or
`WM_SETTINGCHANGE` (`SPI_SETWORKAREA`). Disconnecting/reconnecting a
monitor, changing resolution, or docking/undocking a laptop isn't detected
at all - a monitor's tiling tree ends up orphaned (referencing a stale
monitor handle) with no automatic reconciliation on reconnect. Plausible
real trigger given this user's laptop + secondary monitor setup.

**Reference:** research doc sections "`WM_DISPLAYCHANGE`",
"`WM_SETTINGCHANGE`", "Hidden message-window events".

**Plan (bigger lift than the others - needs new machinery):**
- Create a hidden message-only window (own window class + WndProc) in the
  tiling daemon, following GlazeWM's `MessageWindow` pattern.
- Handle `WM_DISPLAYCHANGE` and `WM_SETTINGCHANGE` (filtered to
  `SPI_SETWORKAREA`) by re-enumerating monitors and reconciling: drop trees
  for monitors that no longer exist (re-inserting their windows onto the
  nearest/primary remaining monitor), and reflow everything.
- Decide whether this hidden window's message pump can share the existing
  `GetMessageW` loop thread in `events.py` (likely yes, since window
  messages for a window created on that thread are delivered there too) or
  needs its own thread.

---

## 5. [ ] Check `SetWinEventHook` registration failures

**Module:** `src/oriel/tiling/events.py`

**Priority: MEDIUM**

**Problem:** `run_message_loop()` in `events.py` doesn't verify each hook
handle returned by `SetWinEventHook` is valid before continuing. Compare to
`drag/daemon.py`'s `run()`, which does `if not hook_handle: raise
ctypes.WinError()`. If a hook silently fails to register, tiling runs in a
partially-broken state with nothing surfaced anywhere.

**Reference:** research doc: "Surface registration failures instead of
running with only part of the event model active."

**Plan:**
- After each `SetWinEventHook` call in `run_message_loop()`, check the
  returned handle and raise (matching `drag/daemon.py`'s existing pattern)
  if it's falsy/invalid, instead of silently continuing.

---

## 6. [ ] Add `WINEVENT_SKIPOWNPROCESS` to tiling's hooks

**Module:** `src/oriel/tiling/events.py`

**Priority: LOW-MEDIUM**

**Problem:** tiling's own `reflow()` `SetWindowPos` calls generate
`EVENT_OBJECT_LOCATIONCHANGE` events that tiling's own hook then has to
filter through (cheap today - one dict membership check in
`recheck_if_pending` - but unnecessary self-triggered noise).

**Reference:** research doc: GlazeWM registers every hook range with
`WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS`.

**Plan:**
- Add `WINEVENT_SKIPOWNPROCESS` to the flags passed to every
  `SetWinEventHook` call in `events.py`.
- Double check this doesn't accidentally suppress anything oriel actually
  needs from its own process (it shouldn't - drag.py and tiling.py are
  separate processes, so this only filters out tiling's own generated
  events, not drag.py's).

---

## 7. [ ] Reclaim layout space when a window is minimized

**Module:** `src/oriel/tiling/events.py`, `src/oriel/tiling/state.py`

**Priority: LOW-MEDIUM (product decision, not just a bug)**

**Problem:** oriel doesn't hook `EVENT_SYSTEM_MINIMIZESTART`/
`EVENT_SYSTEM_MINIMIZEEND`. Minimizing a tiled window currently leaves its
tile slot as blank space instead of the other windows expanding to fill it,
until the window is closed or restored.

**Reference:** research doc sections "`EVENT_SYSTEM_MINIMIZESTART`",
"`EVENT_SYSTEM_MINIMIZEEND`".

**Plan (discuss desired behavior before implementing):**
- Decide: should a minimized window temporarily leave the tree (like a
  hide) and get re-inserted at/near its old position on restore, or should
  it just be visually skipped but still reserve ratio space (current
  behavior)?
- If temporary removal is desired, hook MINIMIZESTART/MINIMIZEEND similarly
  to the hide/cloak handling in task 2, remembering enough position info
  to reinsert sensibly on restore.

---

## 8. [ ] Keep `_focused_leaf` in sync with real OS focus

**Module:** `src/oriel/tiling/events.py`

**Priority: LOW**

**Problem:** oriel doesn't sync its per-monitor "focused leaf" with real
`EVENT_SYSTEM_FOREGROUND` changes beyond the narrow pending-manageable
recheck use in `recheck_if_pending`. If the user Alt-Tabs directly instead
of using the tiling hotkeys, `focus_direction`/`move_direction` could act
relative to a leaf that isn't actually focused anymore.

**Reference:** research doc: "`EVENT_SYSTEM_FOREGROUND`" - "Synchronize
native and WM focus, update the focused descendant."

**Plan:**
- On every `EVENT_SYSTEM_FOREGROUND`, look up the leaf for the newly
  foregrounded hwnd (if any) and update `_focused_leaf` for its monitor,
  independent of the existing pending-recheck logic.

---

## 9. [x] Monitor-settle reassert fights a real drag gesture within its settle window

**Module:** `src/oriel/tiling/events.py`

**Priority: HIGH (confirmed regression risk in task 3's own fix)**

**Status:** Fixed and verified (2026-08-13). Task 3's fix
(`_reassert_monitor_if_settling`) had no awareness of real interactive
move/resize gestures - it only checked "is this hwnd's real monitor
different from its tree-recorded monitor", with zero regard for *why*.
Confirmed live: opening a new window and, within its 2s settle window,
doing a properly-bracketed drag (`EVENT_SYSTEM_MOVESIZESTART` ... repeated
`SetWindowPos` ... `EVENT_SYSTEM_MOVESIZEEND`, exactly what `drag.py`
emits for both native OS drags and custom alt+drags) to a second monitor
got silently snapped straight back to the original monitor mid-gesture -
the window never actually moved from the user's perspective. Fixed by
hooking `EVENT_SYSTEM_MOVESIZESTART` in `events.py` itself (previously
only `drag.py`, a separate process, hooked the START side) and tracking
an `_active_gestures` set of hwnds currently inside a bracketed gesture.
`_reassert_monitor_if_settling` now skips entirely while a hwnd is in
`_active_gestures`, so a deliberate user drag is never fought regardless of
timing, while an app's async self-repositioning (which never goes through
a real gesture) is still caught exactly as task 3 intended. Verified live,
before/after: pre-fix the simulated bracketed drag never visibly moved the
window at all; post-fix it lands on the target monitor as expected, and
task 3's original repro (unbracketed async self-move) is still correctly
reasserted.

**Problem (historical):** Root cause was using a purely time-based proxy
("is this within N seconds of the window being shown") to guess at intent
("is this an app auto-repositioning itself, or a deliberate user move")
when a precise, existing Win32 signal (`MOVESIZESTART`/`MOVESIZEEND`
gesture bracketing) was available and simply not being consulted at the
tiling-daemon layer.

---

## 10. [ ] Consolidate the duplicated hwnd-tracking/retry mechanisms in events.py

**Module:** `src/oriel/tiling/events.py`

**Priority: LOW (architecture/maintainability, not a user-facing bug)**

**Problem:** `events.py` has independently hand-rolled three separate
"track a hwnd with a TTL or retry budget, recheck opportunistically, clean
up on destroy" mini-systems: the pending-manageable retry
(`_pending_manageable_hwnds`/`_manageable_retry_counts`/
`_manageable_retry_scheduled` + `threading.Timer`), the monitor-settle
reassert (`_settling_hwnds`, now also `_active_gestures`), and the
recently-finalized drag dedup (`_recently_finalized`). Each has its own
tracking dict(s), its own expiry style (eager timer vs. lazy
check-on-next-event), and its own cleanup calls scattered across
`on_window_destroyed`/`on_window_hidden`. Task 7 (reclaim space on
minimize) will likely need a fourth near-identical mechanism if
implemented as currently scoped.

**Plan:**
- Design a single small reusable abstraction (e.g. a `TimedHwndTracker`
  class: add hwnd with a TTL/retry budget, check-and-expire, one shared
  cleanup call) and migrate the existing three mechanisms onto it, so
  task 7's minimize-tracking can reuse it instead of adding a fourth
  bespoke copy.
- Do this as a pure refactor with no behavior change - verify via the
  existing repro scripts/tests for tasks 1-3 and 9 still pass identically
  before considering it done.

---

## 11. [ ] Reduce events.py's WinEventHook dispatch/registration boilerplate

**Module:** `src/oriel/tiling/events.py`

**Priority: LOW (architecture/maintainability, not a user-facing bug)**

**Problem:** `_win_event_proc`'s event-to-handler dispatch is an
ever-growing `if/elif` chain, and `run_message_loop` has one named local
per hook (`hook_show`, `hook_destroy`, ... `hook_uncloaked`) with a
matching `if hook_x: UnhookWinEvent(hook_x)` line repeated in `finally`.
Both scale linearly and repetitively as more event types are added - task
4 (display config changes) needs an entirely new hidden-message-window
event source, and task 7 (minimize/restore) adds two more WinEvent types.

**Plan:**
- Replace the `if/elif` dispatch with a declarative table (event constant
  -> handler function) that `_win_event_proc` looks up.
- Replace the per-hook named locals with a list of (event_lo, event_hi)
  ranges registered/unregistered in a loop.
- Give task 4's hidden-message-window event source (a structurally
  different mechanism than `SetWinEventHook`) its own module rather than
  bolting it into the WinEventHook-shaped code in `events.py`.

---

## 12. [x] Enforce tiled placement unconditionally, not just within a settle window

**Module:** `src/oriel/tiling/events.py`

**Priority: HIGH (explicit product decision)**

**Status:** Implemented and verified (2026-08-13). Explicit product
decision: apps must never manage their own window boundaries once oriel
has tiled them - oriel is the sole authority on tiled window geometry, for
the window's entire lifetime, with exactly one exception: an active
move/resize gesture (the user dragging it). This generalizes/supersedes
the time-bounded aspect of tasks 3 and 9: `_reassert_monitor_if_settling`
(bounded to `MONITOR_SETTLE_WINDOW` after a window was first shown, and
only checking monitor identity) is replaced by `enforce_tiled_placement`,
which runs on every `EVENT_OBJECT_LOCATIONCHANGE`/`EVENT_SYSTEM_FOREGROUND`
for the hwnd's entire tiled lifetime and checks BOTH monitor identity and
full rect (frame-expanded) against what the tree currently expects,
reflowing immediately if either has drifted. Still gated on
`_active_gestures` (from task 9) so a real drag/resize is never fought.
Fullscreen leaves are explicitly exempted (their rect is intentionally the
full monitor bounds, not the tiled split rect). This also simplified the
code: `_settling_hwnds`/`MONITOR_SETTLE_WINDOW` and their cleanup calls are
gone entirely - one fewer of the duplicated hwnd-tracking mechanisms task
10 was concerned about.

**Concrete bug this fixed:** discovered live while investigating "Firefox
doesn't get tiled" - oriel correctly split Firefox alongside another
tiled window at first (verified via reflow tracing: right-half rect
applied and confirmed via `GetWindowRect` immediately after), but Firefox
then asynchronously resized/repositioned *itself* back to ~full-monitor
size shortly after, on the *same* monitor - which `_reassert_monitor_if_settling`
never noticed, since it only ever compared monitor identity, never rect.
Verified live before/after: pre-fix, Firefox ended up overlapping the
other tiled window at full width; post-fix, it stays exactly at its
tree-assigned split rect. Also re-verified a real bracketed drag gesture
(constant window size + cursor moving together, matching drag.py's own
technique) is still fully respected and never fought.

**Accepted tradeoff (explicitly approved, not an oversight):** if some app
has a persistent internal habit of resizing itself outside of a user drag
(e.g. an aggressive "always maximize" timer), oriel will now fight it
indefinitely rather than just within a short window after opening -
previously avoided deliberately (see this session's prior notes on
"fighting" bugs), but explicitly requested here: *"I really dont want
apps to manage their own boundaries at all so I am fine with enforcing
their tiled positions at all times. I just need an exception for when
dragging them around."*

---

## 13. [x] Windows with an enforced minimum size fight task 12's unconditional enforcement

**Module:** `src/oriel/tiling/tree.py`, `src/oriel/tiling/state.py`, `src/oriel/tiling/geometry.py`, `src/oriel/tiling/events.py`

**Priority: CRITICAL (self-inflicted regression from task 12, causes an active fight loop, not just a cosmetic gap)**

**Status:** Implemented and verified (2026-08-17). Per explicit product
direction: minimum sizes should be *learned* (not speculatively queried)
and fed into the layout so siblings automatically shrink to accommodate a
window's real floor (borrowing space proportionally, same as a manual
ratio resize) - and when there isn't enough space to satisfy every
minimum even after borrowing, the layout should overflow (spill past the
work area) rather than keep renegotiating forever.

**Design:**
- `geometry.py`: new `_min_size_cache` (hwnd -> (min_w, min_h), in the
  same frame-inclusive coordinate space as `GetWindowRect`/`SetWindowPos`).
  `learn_min_size(hwnd, w, h)` grows the recorded floor (monotonic
  `max()`) and returns True only if it changed; `min_size(hwnd)`;
  `invalidate_min_size(hwnd)` (called from `state.remove_leaf`, alongside
  the existing frame-margin cache invalidation).
- `tree.py`: `compute_rects` gained an optional `min_sizes` parameter
  (`{item: (min_w, min_h)}`, default `{}` - fully backward compatible,
  byte-identical output to before when no minimums are known). New
  `_subtree_min(node, axis, gap, min_sizes)` computes a subtree's own
  minimum along an axis: a Leaf's known minimum, or for a Container -
  summed (+gaps) if it splits along that axis, else the max of its
  children (they're stacked on the cross axis, so each already gets the
  container's full span there - verified with a dedicated nested-container
  unit test). New `_distribute_sizes(available, ratios, minimums)`
  converts ratios to pixel sizes, borrowing space proportionally from
  siblings with slack to cover a deficient child's floor; if total
  minimums still don't fit even after borrowing everyone's slack, sizes
  simply don't sum to `available` (an honest overflow, not hidden or
  forced). 4 new unit tests cover: minimums that don't need to kick in,
  borrowing across a simple 2-child split, the nested-container max-not-
  sum case, and the over-constrained/overflow case.
- `state.py`: new `TilingState.compute_rects(monitor, workspace)` helper
  (builds the `min_sizes` dict from `geometry.min_size` for every current
  leaf, then calls `tree.compute_rects`) - used by `insert_hwnd`, `reflow`,
  and `events.enforce_tiled_placement`, so all three always agree on the
  same min-size-aware target rect. `reflow()` now also compares each
  window's actual post-`SetWindowPos` rect against what was requested;
  if the OS/app clamped it larger, `geometry.learn_min_size` records it,
  and (only if that taught something new) `reflow` re-runs once so the
  layout redistributes around the newly-known floor - bounded by a new
  `MAX_MIN_SIZE_LEARN_PASSES = 5` (mirrors `MAX_MANAGEABLE_RETRIES`'s
  existing bounded-retry pattern) so it can never chain indefinitely.
- `events.py`: `enforce_tiled_placement` now also refuses to re-trigger
  `reflow()` when a mismatch is just "clamped bigger than requested" and
  `learn_min_size` reports nothing new - otherwise every future
  `LOCATIONCHANGE` for an already-at-its-floor window would keep asking
  for the identical impossible size and re-detecting the identical
  mismatch, fighting forever from the *outside* even with `reflow()`'s own
  internal recursion bounded.

**Debugging notes (both bounds above were necessary, not just one):**
An earlier attempt that let `_distribute_sizes` borrow space unboundedly
and let `reflow()`/`enforce_tiled_placement` retry unboundedly reproduced
a genuine sustained fight (~8-10 reflow calls/sec, never settling) once
enough windows (Firefox + 5 Notepads on one monitor) collectively demanded
more floor space than was available. Root cause was the *combination* of
unbounded cross-invocation retriggering (`enforce_tiled_placement` calling
`reflow()` again every time a LOCATIONCHANGE arrived, even for an already-
known, unresolvable minimum-size mismatch) - fixed by both bounding
`reflow()`'s own internal learn-and-retry recursion AND making
`enforce_tiled_placement` recognize "this exact mismatch teaches nothing
new" and stop re-triggering. Verified live (before/after): pre-fix, the
same 6-window scenario produced continuous, never-settling reflow calls;
post-fix, reflow calls drop to exactly 0/sec once every window's minimum
is learned, with the final layout correctly overflowing (Firefox ~698px +
5x Notepad ~457px = ~2983px total on a 2560px monitor) instead of fighting.
Also confirmed Firefox does *not* always enforce a hard minimum via
`WM_GETMINMAXINFO` for programmatic resizes - in one test run it happily
shrank to 16px wide with no clamp at all, contradicting this task's
original "Firefox is a known example" assumption; Notepad reliably does
enforce one (~443x1363) and was used as the primary verification vehicle
instead.

---

## Not in scope for tiling, noted for later

- `src/oriel/drag/daemon.py`'s `WH_MOUSE_LL` low-level mouse hook is exactly
  the mechanism GlazeWM deliberately avoids (uses raw input /
  `RegisterRawInputDevices` instead), because Windows can silently disable a
  slow low-level hook. Worth a separate conversation about drag.py's
  robustness if this becomes a real problem in practice.
