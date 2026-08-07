# webuse reliability — research findings & recommendations

## 1. Executive summary — highest-leverage moves

1. **Make `type` guarantee/verify focus before injecting keystrokes** (honor `s.id`) — kills the single worst silent failure where keys land in the wrong field but return `ok:true`. `dispatch.py`, `input.py`.
2. **Force `AXManualAccessibility` per pid** — one call unlocks the entire web-content control tree for Electron/Chromium apps (Slack, VS Code, Discord, Figma, Notion, Teams), which are near-blind to the agent today. `ax.py`.
3. **Batch per-node AX reads** into one `AXUIElementCopyMultipleAttributeValues` call — ~3× fewer Mach round-trips per node, less truncation, and makes every future "read one more attribute" free. `ax.py`.
4. **`activate()` must poll for frontmost readiness and `dispatch()` must not swallow `raise_window` failure** — stops input landing on whatever app is frontmost after a slow launch. `input.py`, `dispatch.py`.
5. **Stop reporting `ok:true` as "effect confirmed"** — add `verified`/`changed` fields + `AXValue` readback on `set_value`/toggles. `ax.py`, `dispatch.py`.
6. **Correlate video + DOM with a shared seq/timestamp and add an atomic `/snapshot` endpoint** — eliminates the core correctness hazard of reading a DOM that describes a different instant than the pixels. `capture.py`, `app_routes.py`, `useDriver.ts`, `sdk.ts`.
7. **Add a flat, token-cheap `?format=text` ghost-DOM serialization** — the representation LLM agents actually consume best; purely additive. `dom.py`, `app_routes.py`, `action_routes.py`.
8. **Replace the blind 80ms/130ms settle sleeps with a bounded AX-quiescence wait** + a first-class `wait` action — makes the returned DOM trustworthy without slowing instant apps. `action_routes.py`, `dispatch.py`, `watch.py`.

## 2. Quick wins (P0, small/medium effort, high impact)

| change | files | impact | effort |
|---|---|---|---|
| `type` honors `s.id`: set `AXFocused`/click-to-focus, else refuse when nothing editable focused | `dispatch.py:47-51`, `input.py:150-161` | Kills keystrokes-to-wrong-field silent failure | small |
| `activate()` polls frontmost until pid matches; `dispatch` returns `activation_failed` (stop discarding `raise_window` bool) | `input.py:76-81`, `dispatch.py:27-29` | Stops input on wrong app after slow launch/focus-steal | small |
| Force `AXManualAccessibility` once per pid | `ax.py` (snapshot pre-walk) | Unlocks all Electron/Chromium web content | small |
| Batch ~10 per-node attrs into one `CopyMultipleAttributeValues` | `ax.py:229-279` | ~3× less IPC, less truncation | medium |
| Pass raw AX role/subrole through as `GhostNode.role` | `dom.py:6-35`, `api.ts:51-68`, `ghost.ts` | Disambiguates slider/tab/combobox/row | small |
| Window-relative `x,y` uses live window origin, not cached snapshot | `dispatch.py:15-18,22,68-71` | Coordinate clicks land after window move | small |
| `set_value` readback + `verified`/`changed` fields; `dispatch` marks coord-click/type `verified:false` | `ax.py:311-335`, `dispatch.py:57-78` | Honest "did it take" signal | small–med |
| `?format=text` flat DOM serializer | `dom.py`, `app_routes.py`, `action_routes.py` | Best LLM-consumable grounding | small |
| Stable machine-readable error `code`s + AXError decode map | `dispatch.py`, `ax.py:328`, `action_routes.py` | Programmatic retry/replan | small |
| Per-app `AXUIElementSetMessagingTimeout(~0.75s)` | `ax.py:11/194`, `windows.py:86,126,146` | One hung app can't truncate whole DOM | small |
| Shared frame seq + atomic `/snapshot` endpoint; flag stale re-served frames | `capture.py:191-270`, `app_routes.py:75-79`, `sdk.ts` | Removes video/DOM desync correctness hazard | medium |

## 3. Per-theme recommendations

### 3.1 Input reliability

**P0 — `type` must guarantee/verify focus before injecting keystrokes.** When `s.id` present: resolve via `ax._lookup`, set `AXFocused=True` (same pattern as `set_value` at `ax.py:324`) or HID-click its center, then type. When absent: read `AXFocusedUIElement`; return `{ok:false,error:'no_focused_field'}` if nothing editable is focused instead of typing blind. `Step.id` already exists (`models.py:8`) but the type branch ignores it.
- *Rationale:* type currently trusts a prior click landed focus with no check — top-cited defect. *Impact:* high (every form-fill/search). *Effort:* small. *Risk:* low (mirror `set_value`; keep HID typing so AppKit keydown handlers fire; fall back to click-to-focus if element rejects `AXFocused`).
- *Evidence:* `dispatch.py:47-51`, `ax.py:324`, `input.py:150-161`.

**P0 — `activate()` polls frontmost; `dispatch()` honors activation/raise failure.** Replace the flat `time.sleep(_SETTLE)` (`input.py:76-81`) with a bounded poll (~1s in 20-30ms steps) of `NSWorkspace.frontmostApplication`/`app.isActive`; return a bool. In `dispatch.py:27-29` capture both `activate()` and `windows.raise_window()` bools and return `{ok:false,error:'activation_failed'}` before posting any HID.
- *Impact:* high. *Effort:* small. *Risk:* low (fast path returns faster than today's flat 0.12s; make timeout a named constant for cold launches). *Evidence:* `input.py:76-81`, `dispatch.py:27-29`.

**P0 — Window-relative `x,y` uses live origin.** Replace `ax.last_window(...)['x'/'y']` in `dispatch.py:15-18,22,68-71` with a live `windows.resolve_window` + `ax._geom(win)` (or a thin `windows.live_origin`), mirroring what id-clicks already get via `ax.center`'s live `_geom`.
- *Impact:* high (coordinate clicks after ~250ms+ window move). *Effort:* small. *Risk:* low. *Evidence:* `dispatch.py:15-18,22,68-71`, `ax.py:211,285-286`, `windows.py:142`.
- *Note:* the broader "id-clicks are stale" framings were **rejected** — `ax.center()` does a live `_geom` read every dispatch; only the AXUIElement ref is cached (CFEqual-stable by design). The x,y path is the only genuine stale-geometry bug.

**P1 — Pre-click actionability checks + one bounded retry.** In `dispatch.py:57-64`: re-read `AXEnabled` (fail fast if disabled); hit-test the resolved global point with `AXUIElementCopyElementAtPosition` and CFEqual-compare against the target (Playwright's "Receives Events") → `{ok:false,error:'occluded'}` if covered. Wrap resolve+check in a bounded retry (~1-2s, poll ~50-100ms).
- *Impact:* high (transient disabled/settling races become successes; catches click-through). *Effort:* medium. *Risk:* medium (extra AX round-trip/click; cap tightly; add `AXUIElementCopyElementAtPosition` import). *Evidence:* `dispatch.py:52-78`, `action_routes.py:50-57`, `ax.py:299-335`.

**P1 — Occlusion-aware click point: intersect element frame with scroll-ancestor clip rect.** In `ax.center()` (`ax.py:299-308`) intersect element `_geom` with nearest `AXScrollArea` visible frame (reuse `_scroll_area_at`, `ax.py:338-358`); click centroid of intersection; empty → return None so dispatch surfaces "scroll into view first."
- *Impact:* medium (partially-scrolled rows whose full AX frame center falls over a header). *Effort:* medium. *Risk:* medium (extra descent; fall back to raw center if no scroll ancestor). *Evidence:* `ax.py:299-308,167-180,338-358`.

**P1 — Short move→mouseDown settle gap in `click()`/`drag()`.** Add ~15-25ms between `move()` and first `_post_mouse(down)` (`input.py:104-107,114-115`); named constant like `_SETTLE`.
- *Impact:* medium (fixes intermittent first-click miss on hover-dependent AppKit/WebKit/Electron controls). *Effort:* small. *Risk:* low. *Evidence:* `input.py:99-120,66`.

**P1 — Adaptive AX-quiescence settle barrier** (replace flat 80ms). `action_routes.py:51-53`: poll for tree quiescence (node-count/geometry stable across two reads or ~500ms cap); reuse the P1-retry helper so fast path returns immediately.
- *Impact:* high (batch steps after a window/sheet-opening click). *Effort:* medium. *Risk:* medium (snapshot cost; cap tightly). *Evidence:* `action_routes.py:51-53`.

**P1 — Wire `ax.scroll()` as fallback + accept `s.id` for scroll.** `dispatch.py:65-66` currently HID-wheel only. Accept `s.id`; fall back to `ax.scroll(pid,gx,gy,dx,dy,window_id)` (`ax.py:371-387`, **dead code, never called**) when HID wheel resolves to no scrollable region. AX-scrollbar path is occlusion/origin-free.
- *Impact:* medium. *Effort:* medium. *Risk:* low-med (needs `AXScrollBar`; keep HID wheel as general path). *Evidence:* `dispatch.py:65-66`, `ax.py:371-387`.

**P1 — `set_value` read-back verify + HID keystroke fallback.** `ax.py:321-325`: after `AXValue` write, re-read; on mismatch fall back to activate→click-to-focus→cmd+a→`input.type_text`. webuse uniquely has full HID injection to do this.
- *Impact:* medium (rich/attributed NSTextView, web text areas where AXValue silently no-ops). *Effort:* medium. *Risk:* medium (only trigger on verified mismatch). *Evidence:* `ax.py:321-325`, `dispatch.py:47-51`.

**P2 items:** chunk long `type_text` (~50-80 chars) and consider raising per-char delay toward Anthropic's 12ms/`TYPING_GROUP_SIZE=50` (`input.py:150-161`); reject/clamp out-of-window-bounds `x,y` (`dispatch.py:9-18`, mirror Anthropic `validate_and_get_coordinates`); distance-scale drag interpolation with small initial steps + optional stepped scroll (`input.py:64-65,110-131`); tag synthetic events with `eventSourceUserData` + optional diagnostic `CGEventTap`, neutral `virtualKey` sentinel instead of `0`/"A", VM `CGWarpMouseCursorPosition` fallback (`input.py:53-54,84-107,155-158`).

**Rejected/downgraded:** move-before-click ordering already satisfied (`input.py:104,114`); stale-id already fails loudly (`dispatch.py:54-55` → clear error), so retry work targets transient/occlusion misses, not stale ids.

### 3.2 Ghost DOM & AX

> **Sequencing:** land batched-attribute fetch FIRST — it makes every "read one more attribute" (focused/selected/expanded/min/max/required/invalid) free (same single IPC call). The three P0s are independent and low-risk; do them together.

**P0 — Batch per-node AX reads.** `ax.py:229-279`: replace ~10 separate `_attr()` calls (AXRole, AXSubrole, AXTitle, AXDescription, AXValue, AXPlaceholderValue, AXHelp, AXEnabled) + `_geom`'s AXPosition/AXSize with one `AXUIElementCopyMultipleAttributeValues(el, attrs, 0, None)` (import-confirmed in `.venv`). Keep AXChildren/AXVisibleRows/Cells and `CopyActionNames` separate; still unwrap position/size via `AXValueGetValue`; read role from batched result to keep the `role==None` skip.
- *Impact:* ~12→~4 Mach round-trips per node (~3×); less truncation. *Effort:* medium. *Risk:* low-med (per-attr error placeholders → treat as None). *Evidence:* `ax.py:146-148,229-279,26/31`.

**P0 — Force `AXManualAccessibility` per pid.** Once per pid: `AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), 'AXManualAccessibility', True)`, swallow AXError. Case-agnostic, no allowlist. Existing 2s `SAFETY_NET_S` re-walk (`watch.py`) catches the lazily-populated tree.
- *Impact:* highest impact-per-line; unlocks Slack/VS Code/Discord/Figma/Notion/Teams web content. *Effort:* small. *Risk:* low (native apps → swallowed AXError; first snapshot may be thin, safety-net covers). *Evidence:* grep confirmed zero refs to `AXManualAccessibility`/`AXEnhancedUserInterface`/`AXWebArea`.

**P0 — Pass through raw AX role/subrole as `GhostNode.role`.** `dom.py classify()`: add `role` to every dict (or forward raw AXRole); add `role:string` to `GhostNode` (`api.ts:51-68`); emit `data-role` in `ghost.ts build()`. Zero extra AX calls (already walked, `ax.py:269-270`).
- *Impact:* today everything collapses to text/press/static — agent can't tell slider from stepper from tab. *Effort:* small. *Risk:* low (additive). *Evidence:* `dom.py:6-35`, `ax.py:269-270`, `api.ts:51-68`.

**P1 — Stop dropping numeric AXValue in classify fallback.** `dom.py:33-35`: after the `isinstance(v,str)` branch add `if v is not None and not isinstance(v,bool): return {'kind':'static','tag':'span','text':str(v)}` so value-bearing controls (progress/meter/rating/custom) surface instead of vanishing.
- *Impact:* medium (closes a silent-invisibility class). *Effort:* small. *Risk:* low. *Evidence:* `dom.py:33-35`, `ax.py:183-191`.

**P1 — Slider/stepper coverage + operability.** `dom.py`: branches for `AXSlider`/`AXValueIndicator` → `{kind:'slider',role,value,min,max}` and `AXIncrementor`/`AXStepper` → `{kind:'stepper'}` (fold AXMinValue/AXMaxValue into the batched read). `dispatch.py`/`ax.perform()`: add `increment`/`decrement` → AXIncrement/AXDecrement, and make numeric `set_value` pass a real number (not the `str()` forced at `ax.py:325`, which breaks numeric AXValue targets).
- *Impact:* turns invisible+unoperable volume/zoom/rating into first-class targets. *Effort:* medium. *Risk:* medium (numeric coercion; graceful axerror fallback). *Evidence:* `dom.py:6-35`, `ax.py:325`, `dispatch.py:32-35`.

**P1 — Surface selection/expansion state.** Fold `AXSelected`/`AXExpanded`(`AXDisclosing`) into the batched read. `dom.py`: `AXPopUpButton`/`AXMenuButton` → `{kind:'menubutton',value:currentSelection}`; `AXTab` → `selected:bool`; `AXDisclosureTriangle` → `expanded:bool`. Thread through `build_nodes` + `GhostNode`.
- *Impact:* medium (tab strip / disclosure / popup read-back — "clicked but did it work"). *Effort:* medium. *Risk:* low once batched. *Evidence:* `dom.py:30-32,38-39`.

**P1 — Make rows selectable via existing AXPick path.** `dom.py`: `role in {AXRow,AXCell}` and `{AXPress,AXPick,AXSelect} & actions` → `{kind:'press', text: aggregate child AXStaticText/AXCell labels}`. `ax.perform()` already has the AXPick fallback (`ax.py:317-320`) but no classify path reaches it.
- *Impact:* Mail messages / Finder rows / playlist tracks become actionable. *Effort:* medium. *Risk:* low. *Evidence:* `dom.py:18-35`, `ax.py:316-320`.

**P1 — Surface keyboard focus (`GhostNode.focused`).** Fold `AXFocused` into batched read, or reuse `watch._track_focused` (`watch.py:186`, already resolves `AXFocusedUIElement`): mark matching node id `focused:true`; thread through `build_nodes`/`GhostNode`/`ghost.ts data-focused`.
- *Impact:* direct "did my click land" affordance before sending keys. *Effort:* small. *Risk:* low. *Evidence:* `watch.py:180-194`, `ax.py:264-279`, `api.ts:51-68`.

**P2 items:** non-interactive `AXScrollArea` marker (**must** be `pointer-events:none` — ghost default is `auto`, or it swallows child clicks) for scroll discoverability (`dom.py`, `ghost.ts`/`index.css`, `ax.py:371-387`); surface `AXRequired`/`AXInvalid` on form fields (fold into batched read); incremental snapshotting for scoped notifications (**defer** until batching measured — `watch.py:205-207`); opt-in vision/OCR fallback for AX-blind canvas/WebGL views (reassess after `AXManualAccessibility` shrinks scope); advertise the existing `truncated` flag to the SDK surface (**backend already ships it** at `app_routes.py:77` and `watch.py:213` — plumbing/UX check only).

### 3.3 Rendering & streaming

> **Judgment:** frame/DOM correlation + atomic snapshot (P0) is higher leverage for agent *reliability* than the H.264 rewrite — an agent acting on desynced two-stream state is a correctness bug, whereas client-paced-pull already bounds JPEG latency acceptably.

**P0 — Correlate video+DOM with shared seq/ts + atomic `/snapshot` + stale-frame flag.** `capture.py`: stamp each JPEG with a monotonic seq (single shared counter); mark re-emitted frames in the error path (`capture.py:268-269` "yield last") stale so they don't advance fresh-seq. `app_routes.py`: send seq as preamble/1-byte flag on `stream_ws` binary frames; add seq/ts to `GET /app/{handle}/dom` and the SSE payload. Add `GET /app/{handle}/snapshot` → `{jpeg,dom,seq}` captured back-to-back under one lock (reuse last frame + fresh walk so it doesn't stall capture). `useDriver.ts`/`sdk.ts`: expose current seq + `snapshot()`/`getState()`.
- *Impact:* high (removes core hazard: DOM state ≠ pixel state during animation/resize/post-action). *Effort:* medium. *Risk:* low (additive; JPEG-only/SSE clients unaffected). *Evidence:* `capture.py:191-226,264-270`, `app_routes.py:65-79`, `useDriver.ts:377-420 vs 476-485`.

**P0 — Per-app AX messaging timeout.** `AXUIElementSetMessagingTimeout(app_el, ~0.75)` after each `AXUIElementCreateApplication` (`ax.py:11/194`, `windows.py:86,126,146`). `_attr` already treats non-zero err as None → fails safe.
- *Impact:* medium-high (a single blocked `CopyAttributeValue` can burn seconds and truncate the tree; `WALK_BUDGET_S` only checked *between* nodes). *Effort:* small. *Risk:* low. *Caveat:* verify `AXUIElementSetMessagingTimeout` imports from ApplicationServices/HIServices in installed pyobjc. *Evidence:* `ax.py:31,146-148`, `windows.py:61`.

**P1 — Suppress input + hold ghosts for one frame after a window-size change.** `useDriver.ts render()` (161-167): on size change set a "resize pending" flag; clear in `onScreenLoad` (447) once img loads at new dims. While pending, `rel()` (182-189) drops/ignores mouse+scroll (or compute the true `object-fit:contain` rect from `naturalWidth/Height` vs box) and defers `applyScale`/`sync`.
- *Impact:* medium (during resize, `#stage` is sized to new winW/H while `#screen` still paints old-res frame → letterbox → mislanded clicks + ghost drift). *Effort:* medium. *Risk:* low-med (contain-rect math is the fiddly part). *Evidence:* `useDriver.ts:161-167,182-189,421-434`, `ghost.ts:57-60`, `index.css #screen`.

**P1 — Wire `encoder.py` as opt-in H.264 (HEVC-upgrade) WebCodecs stream — never drop mid-GOP.** Add `/app/{handle}/stream_video_ws` mirroring `stream_ws` (`app_routes.py:124-197`) sourcing `Encoder(codec,fps).access_units(cap.jpegs())`; send init with `codec_string()`/sps/pps/vps. Frontend: `VideoDecoder`+canvas path parallel to the `<img>` Blob path (`useDriver.ts:395-404`), feature-detected via `VideoDecoder.isConfigSupported`, **default H.264, accept HEVC only after `isConfigSupported` true** (invert the "H.265-first" memory note for the browser leg). **CRITICAL:** do NOT reuse the slot-overwrite drop pattern (`app_routes.py:158-171`) for AUs — delta AUs aren't independently decodable; drop whole GOPs at keyframe boundaries only, or force a fresh IDR on backlog. Keep JPEG WS as guaranteed fallback.
- *Impact:* medium-high (inter-frame coding → higher fps/lower latency over tunnel). *Effort:* large. *Risk:* medium-high (mid-GOP drop = "random glitch"; HEVC-first = decode failure on many Chromium builds — both avoided by the constraints). *Evidence:* `encoder.py:138-192` (zero external callers, dead code), `app_routes.py:124-197,158-171`, `useDriver.ts:367-427`.

**P1 — Use client-pull ping interval to adapt fps/quality per connection.** `app_stream_ws` (`app_routes.py:174-192`): measure inter-`receive()` interval; nudge `WindowCapture` fps/quality (plain fields, `capture.py:65-66,72-73`) down when client falls behind, up when it keeps up.
- *Impact:* medium (laggy/remote agent gets same fixed 8fps/0.6 as local). *Effort:* small. *Risk:* low (bound range to avoid oscillation). *Evidence:* `capture.py:65-66`, `app_routes.py:174-192`.

**P2 items:** skip re-sending perceptually-identical frames + burst fps briefly after an action (`capture.py:228-273`); `ResizeObserver` on `#stagewrap` instead of only window resize (`useDriver.ts:65`); replace `sync()`'s per-node `JSON.stringify` diff key with render-fields-only compare (`ghost.ts:80`); tag/foreground-raise the degraded `CGWindowList` rect-capture fallback so occluding windows aren't shown as the target (`capture.py:170-189`); optional numbered Set-of-Marks overlay burned into the frame as a vision-only escape hatch (off by default).

**Not actionable:** WebRTC — `tunnel.py` is TCP-only `ssh -R`, no UDP/ICE/STUN/TURN; WebCodecs-over-WebSocket is the right target.

### 3.4 Agent affordances

**P0 — Stop reporting `ok:true` as "effect confirmed"; add honest verification + AXValue readback.** `ax.perform()`: for `set_value`, read AXValue back after write → `{ok:err==0,changed:readback==requested,verified:true}`; for press/confirm on checkbox/radio/menu, capture AXValue/AXSelected before+after → `changed:bool`. `dispatch.py`: add `verified:false` to every coordinate-click/type/key return (distinguish "event delivered" from "effect confirmed").
- *Impact:* high (biggest cited gap; readback plumbing `_attr` already exists). *Effort:* small-med. *Risk:* low (normalize types — compare on `str()`/coerce). *Evidence:* `ax.py:311-335`, `dispatch.py:57-78`.

**P0 — Flat token-cheap text DOM serialization (`?format=text`).** `dom.to_text(nodes)`: one line per renderable node — `[id] role "label" (value/placeholder) [disabled|offscreen]` in reading order. Wire `?format=text` on `GET /app/{handle}/dom` + optional `dom_text` in `/api/action` response. Reuses `build_nodes()` unchanged.
- *Impact:* high (what LLM agents consume best; today every consumer parses `nodes[]` JSON). *Effort:* small. *Risk:* low (additive). *Evidence:* `api.ts DomSnapshot`, `dom.py:42-67`.

**P0 — Stable machine-readable error codes + AXError decode.** `dispatch.py`: `code` on every error (`missing_id`,`no_target`,`unknown_action`,`unknown_key`,`value_required`,`drag_needs_endpoint`,`stale_id`). `ax.perform()`: AXError→reason map (-25204 unsupported, -25202 api_disabled, -25205 precision, -25200 failure) → `{axerror,reason,code}`. `action_routes.py`: surface failing step's `code`.
- *Impact:* medium (programmatic retry/replan without string-matching prose). *Effort:* small. *Risk:* low. *Evidence:* `dispatch.py:34,40,49,55,73`, `ax.py:328`.

**P1 — Replace fixed 80/130ms sleeps with a `wait` action + bounded settle before post-action snapshot.** Add `wait` branch in `dispatch.py` (new `Step.duration` or reuse `dy`). In `action_routes.py action()`: opt-in bounded quiescence before the inline snapshot — poll `watch.DomWatcher` dirty/coalescer or re-snapshot ~50ms until two agree or ~500ms cap → `settled:bool`. Add `webuse.waitForIdle()` to SDK.
- *Impact:* high (SETTLE_MS=130/`sleep(0.08)` return pre-transition DOM exactly when verification matters). *Effort:* medium. *Risk:* medium (cap wait; keep settle opt-in). *Evidence:* `sdk.ts:142,160-164`, `action_routes.py:52-69`, `watch.py:46,140-146`.

**P1 — Server-computed change summary by diffing against watcher's last DOM (no extra walk).** `action_routes.py`: diff post-action `build_nodes` against `watch._watchers[(pid,window_id)].current` (pre-action DOM already held) → `changed` summary (ids added/removed, value/label/enabled changed, focus moved). **Do not add a second `ax.snapshot()`** — it can burn up to `WALK_BUDGET_S=4s`; fall back to `changed:null` if no watcher/subscriber.
- *Impact:* medium-high (one field replaces per-step client tree-diffing). *Effort:* medium. *Risk:* low-med. *Evidence:* `action_routes.py:63-69`, `watch.py:91,224-226`.

**P1 — Consult `AXEnabled` before an id-addressed click.** `dispatch.py`/`ax.center()`: when resolved by `s.id`, check `_attr(el,'AXEnabled')`; if false → `{ok:false,code:'disabled'}`. Only gate the id path (coord clicks have no handle). The disabled flag is already computed in `dom.classify` but decorative on the action path.
- *Impact:* medium (prevents silent no-op clicks returning `ok:true`). *Effort:* small. *Risk:* low-med (scope to id path; pair with `code`). *Evidence:* `dom.py:52-63`, `ax.py:299-308`, `dispatch.py:57`.

**P1 — Echo dispatch result over the input WebSocket (SDK agents get no ack today).** `app_ws`: capture `run_in_executor(dispatch.dispatch,...)` result and `ws.send_json({...result, request id})`. `sdk.ts`/`useDriver.ts send()`: thread a request id, resolve a per-call promise on the echo so `webuse.click()` awaits real ok/error instead of a blind `SETTLE_MS` sleep.
- *Impact:* medium (WS is the low-latency channel the SDK uses; `app_ws:223` throws the result away). *Effort:* small-med. *Risk:* low (UI send() can ignore echoes). *Evidence:* `app_routes.py:200-227`, `useDriver.ts:91-119`, `action_routes.py:50-73`.

**P1 — Broaden the AXObserver notification set.** `watch.py NOTIFICATIONS`: add `AXLayoutChanged`, `AXMenuOpened/Closed`, `AXRowCountChanged`, `AXSelectedRowsChanged`, `AXApplicationActivated` (`AXLayoutChanged` is the common repaint catch-all). Consider raising `SAFETY_NET_S` above 2.0 once covered.
- *Impact:* medium (drops post-action staleness toward COALESCE_S 40ms). *Effort:* small. *Risk:* low (extra notes only trigger the already-debounced re-walk). *Evidence:* `watch.py:34-48,140-146`.

**P2 items:** remove client `refreshSoon()` 250ms poll that races/overwrites the fresher SSE push (`useDriver.ts:110-119,155-178` — verify SSE covers menu-bar presses/drags first); hit-test resolved point via `AXUIElementCopyElementAtPosition` before an id-click for transient-menu occlusion (best-effort warn, not hard-block); system-wide frontmost-app observer (`NSWorkspace didActivateApplicationNotification`) so actions aren't posted to the wrong app mid-trajectory; truncation breadcrumb (`truncated_at:{id,role}`) instead of a bare boolean (`ax.py:220-226,282`).

**Rejected/downgraded:** frame-seq "wait for seq>seq_at_action" contract — `WindowCapture` is per-connection, shares no state with dispatch, and MJPEG `img src` can't carry a header (architecturally infeasible without major plumbing); numbers-burned-into-JPEG (ghost ids already enumerable + text serializer is cheaper); zoom (low value given ghost DOM's textual advantage).

## 4. Bigger bets

| Bet | What | Tradeoffs |
|---|---|---|
| **WebCodecs H.264/HEVC video path** | Wire the already-built (dead) `encoder.py` end-to-end as opt-in `stream_video_ws` + browser `VideoDecoder`. | Inter-frame coding cuts bytes/frame → fresher state for agents. Expensive half already built. **Must** respect GOP boundaries (no mid-GOP AU drops) and negotiate codec (H.264 default, HEVC only after `isConfigSupported`). Keep JPEG WS as fallback. Large effort, medium-high risk. |
| **Push-based SCStream capture → raw pixels to encoder** | Replace `SCScreenshotManager` polling (`capture.py:191-273`) with `SCStream`+`SCStreamOutput` delivering `CVPixelBuffer` at native cadence, fed to `VTCompressionSession` (or `-f rawvideo` instead of `-f mjpeg`, `encoder.py:184-192`). | Removes per-tick screenshot overhead + a full JPEG decode/re-encode per frame. Correct end-state — but sequence AFTER the WebCodecs path proves out. New SCStream lifecycle/threading/TCC/CVPixelBuffer handling. Large, medium-high risk. |
| **Atomic snapshot as the primary agent contract** | Promote `GET /snapshot` (`{jpeg,dom,seq}`) + `getState()` SDK method to the canonical race-free read; agents stop joining two uncorrelated streams. | Cleanest reliability model; may shift agents off the reactive SSE+MJPEG combo. Medium effort; low risk if additive. |
| **Set-of-Marks overlay** | Numbered ordinals burned into the frame, gated/off by default. | Only an escape hatch for vision-only consumers / sparse-AX apps — positioned-HTML-id + `?format=text` already outrank raw-pixel SoM when a real a11y tree exists. Don't make it default. |
| **Incremental snapshotting** | Patch only the changed element on scoped notifications instead of full re-walk (`watch.py:205-207`). | Big win for typing in large docs/spreadsheets, but cache-coherence complexity. **Defer** — batched-attribute fetch removes most of the pain first. |
| **Vision/OCR fallback DOM** | Synthetic ghost nodes over the JPEG for canvas/WebGL/non-AX views. | Real last-resort coverage, but `AXManualAccessibility` removes most Electron/Chromium motivation — reassess scope after that lands. New OCR dep + latency. |

## 5. Cross-cutting notes / open questions

- **Batching is the keystone for Ghost DOM & AX.** Land `AXUIElementCopyMultipleAttributeValues` first — every subsequent attribute (focused/selected/expanded/min/max/required/invalid) becomes free rather than a per-node round-trip. The three P0s (batch, `AXManualAccessibility`, role passthrough) are independent and low-risk; ship together.
- **The recurring theme is silent `ok:true`.** Focus-less type, un-polled activation, blind coordinate origin, unverified `set_value`, disabled-click-through, and video/DOM desync all return success while corrupting the wrong surface — the worst failure mode for an autonomous agent. The verification-field work (Input P0-1/P0-2, Affordances P0-1) is small and the highest reliability ROI.
- **A shared settle/wait helper is reused four times** — build it once (bounded AX-quiescence poll leaning on `watch.py`'s COALESCE_S signal) and reuse for: P1 pre-click retry, adaptive inter-step settle, `wait` action, and post-action snapshot barrier.
- **Import feasibility to verify before implementing:** (a) `AXUIElementSetMessagingTimeout` from ApplicationServices/HIServices in installed pyobjc (fail-safe path already exists, so no other change needed if present); (b) `AXUIElementCopyElementAtPosition` needs adding to `ax.py` imports for hit-testing. `AXUIElementCopyMultipleAttributeValues` is already confirmed importable in `.venv`.
- **Do NOT reuse the slot-overwrite drop pattern for video AUs.** The single most likely way a WebCodecs upgrade "randomly glitches" is dropping individual delta AUs, desyncing the decoder until the next keyframe (~2s glitch). Drop whole GOPs at keyframe boundaries only.
- **`truncated` already reaches agents** (`app_routes.py:77`, `watch.py:213`) — the remaining work is SDK/UI advertising + a breadcrumb, not backend plumbing.
- **Open question — settle vs. latency default.** Should the bounded quiescence wait be opt-in (preserves current latency) or default-on (safer DOM)? Recommend opt-in with a documented `settle`/`settle_ms` param, defaulting off, until real agent traces show how often the pre-transition DOM causes mis-steps.
- **Open question — HEVC-first vs. H.264-first.** The `webuse-nextphase-plan` memory says "H.265-first (H.264 fallback)." For the **browser decode leg** this should invert (H.264 default, HEVC only after `isConfigSupported`) — many Chromium builds fail HEVC decode. Confirm the intended target-browser matrix before wiring.
- **Case-agnostic guardrail respected throughout:** `AXManualAccessibility` is applied per-pid with no app allowlist; no recommendation branches on a specific host/app/bundle-id.