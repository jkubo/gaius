# concord — cross-session coordination

`gaius concord` lets multiple AI coding sessions on one machine **divide work instead of
colliding**. It was born from a real incident: a network outage took a remote
coordination service dark while 11 interactive Claude Code sessions triaged the same
storage cascade — invisible to each other, re-deriving the same diagnosis, overwriting
each other's fixes.

The design lesson: **coordination must live where the sessions live.** concord is a
local, offline-first sidecar — one SQLite file (`~/.gaius/concord.db`), no daemon, no
network dependency. It keeps working when everything else is down, which is exactly
when you have five terminals open.

Why it matters beyond incident response: claims turn "shared mutable everything" into
per-session **ownership**. A session that owns its lane can afford to go deep on it,
instead of defensively re-verifying the whole world every turn.

---

## The four primitives

### 1. Claims — advisory leases

```bash
gaius concord claim subsystem:db --note "running schema migration" [--ttl 14400]
gaius concord claims                # list active leases
gaius concord steal subsystem:db   # deliberate takeover (previous holder is told)
gaius concord release subsystem:db # or --all
```

- **Atomic single-winner**: a partial `UNIQUE` index on active claims — the second
  claimer loses immediately (exit 1) and is told who holds the lease. No queueing.
- **Self-expiring**: a lease dies on TTL (default 4h — a lease is a shift, not a squat)
  or when the holder's pid is gone. Dead sessions cannot squat.
- **Re-claim renews**: claiming a resource you already hold resets the lease clock.
- **Tab retitle**: winning a claim retitles your terminal tab (`⚑ db · session-name`),
  so ownership is visible across a wall of tabs. `--no-title` opts out.
- **Overlap warning**: claims on *differently-named but similar* resources
  (`subsystem:db` vs `subsystem:db-migration`) surface a warning — naming drift is the
  most common way two sessions end up on the same work.

**Resource key conventions**: `subsystem:<name>` (a shared subsystem), `node:<host>`
(a specific machine), `svc:<name>` (a service/namespace), `incident:IC` (incident
commander — first session to claim it runs coordination).

### 2. Findings — shared discoveries with adversarial review

```bash
gaius concord finding add --summary "replica lag is the root cause" \
    --files db/replica.conf --severity major        # info|minor|major|critical
gaius concord finding list [--status open]
gaius concord finding review <id-prefix> --status confirmed   # or refuted|reviewing
```

A discovery one session makes is published for all siblings, and any sibling can
adversarially review it (`open → reviewing → confirmed/refuted`). This is the
max-rigor loop across sessions: one session finds, another verifies.

### 3. Task pool — seeded division of labor

```bash
gaius concord task add "verify backups" --detail "check last 3 snapshots" --resource svc:backup
gaius concord task list | next
gaius concord task take [ID]        # atomic — exactly one winner per task
gaius concord task done ID
```

An incident commander (or you) seeds the pool once; each new terminal takes the next
task atomically. Tasks taken by a session whose pid has died are reclaimed to the pool
automatically.

### 4. Roster — who is alive

```bash
gaius concord roster    # live sessions + the claims each holds
gaius concord status    # one-screen sitrep: sessions · claims · findings · pool
```

The roster merges each coding CLI's own session registry, with pid-liveness layered
on top (registries have no GC):

| Harness | Source |
|---------|--------|
| Claude Code | `~/.claude/sessions/{pid}.json` (`sessionId`, `name`, `status`, …) |
| Grok Build | `~/.grok/active_sessions.json` (`session_id`, `pid`, `cwd`); title from per-session `summary.json` when present |

Self-identity for claims: `CLAUDE_CODE_SESSION_ID` / `GROK_SESSION_ID`, else Grok
pid-ancestry match against `active_sessions` (tool shells often lack the env var),
else `shell-<ppid>`. Roster lines are tagged `[claude]` / `[grok]`.

**Shared helper (do not re-copy the registry):** `gaius.concord._live_sessions(harness=None)`
is the single reader used by roster/status/brief and by the baton/marathon watchers.
Pass `harness="claude"` (or `"grok"`) when a consumer only understands one harness's
transcript layout — baton/marathon still spawn Claude successors today, so they filter
to Claude rather than forking a second `~/.claude/sessions/*.json` scanner.

### 5. Baton pass — hand a saturated session to a fresh successor

```bash
gaius concord handoff --next "verify quorum,re-run playbook 03"   # or pipe a full body on stdin
gaius concord handoff --no-handoff                                # claim→pool transfer only
gaius concord handoff --spawn                                     # + LAUNCH an inert plan-mode successor
```

When a session's context window saturates (the ⛽ context gauge hits 🔴 RED — or ⚫ BLACK,
the "stop now" band above it), pushing more work through a degraded working set is how
plan-drift and confidently-wrong creep in.
`handoff` passes the baton to a fresh context instead. In one command it:

1. **writes a structured handoff** (via `gaius-session-handoff` — a body on stdin, or a
   skeleton from `--next`),
2. **converts this session's active claims into `baton:<resource>` pool tasks** a fresh
   successor can atomically `take` and re-`claim`. The parent's claims are released with
   reason `handed-off`, so nothing is orphaned and no sibling silently double-owns a lane, and
3. **prints the successor bootstrap** — `task next` → `take` → read the handoff → continue.

The successor picks up near the context floor with full reasoning capacity. This is
**model-initiated, never a hook**: the saturated session decides to hand off and runs the
command; nothing auto-relaunches behind the operator's back.

**`--spawn`** goes one step further and *launches* the successor for you — a labeled
`claude --bg --name baton:<skill>:<sid8> --permission-mode plan` session, pre-hydrated with the
handoff via `--append-system-prompt-file`, opening prompt = the compact baton loader (so it
starts near the context floor). It is gated by a **y/N confirmation read off `/dev/tty`** (stdin
carries the piped handoff body); declined / headless / spawn-failure all fall back to a
paste-ready `~/.gaius/baton/<sid>.launch`. Because plan mode is physically read-only and a
`--bg` agent does nothing until you adopt it (`claude /resume "baton:<skill>:<sid8>"`), `--spawn`
**still executes nothing** — it only stages a hydrated successor waiting for you. This is the
model-initiated twin of the `gaius-baton-watch` step-7 spawner; the two build the identical
command (that watcher is the source of truth if they ever drift).

**The baton moves context + claims + capacity — never authorization.** The successor still
hits every gate; a handed-off destructive or irreversible operation still stops for the
operator (the bright line below). `handoff` only moves the baton — it never executes the
handed-off work.

> Terminal cue: the claim tab-retitle (`--no-title`) writes OSC sequences to `/dev/tty`,
> which is a silent no-op on Claude Code ≥ 2.1.139 (hooks/tools run detached from the
> terminal). Saturation is surfaced instead by the `gaius-statusline` context-band bar
> (🟢→🟡→🟠→🔴), which appends `⚑ baton` at ORANGE/RED.

---

## Hook wiring (optional, recommended)

concord is fully usable as a bare CLI, but it shines wired into session hooks. All hook
integration should be gated on a **kill-switch flag file** so you can disable the whole
layer instantly without editing scripts:

```bash
touch ~/.gaius/concord-hooks-enabled     # enable
rm    ~/.gaius/concord-hooks-enabled     # instant kill-switch
```

**SessionStart** — inject the orientation brief (who's live, what's claimed, open
findings, unclaimed pool tasks):

```bash
if [[ -f "$HOME/.gaius/concord-hooks-enabled" ]]; then
    CONCORD=$(timeout 6 gaius concord brief --scope session-start --session "$SESSION_ID" 2>/dev/null)
    # append $CONCORD to your hook's additionalContext output
fi
```

**UserPromptSubmit** — inject only the *delta* since this session's last prompt (new
sibling findings, **new peer claims**, takeovers of your claims). A per-session cursor
guarantees each item is delivered at most once; timestamps are microsecond-resolution so
a finding published the same second as a prompt cannot be skipped:

> **Why peer claims are in the delta (added 2026-07-27).** The full roster renders only at
> `--scope session-start`, so a claim acquired *after* a session started used to be invisible
> to it for the session's entire life — the delta carried findings and steals but never a peer
> acquiring a lane. That is the one event most likely to mean "you are about to duplicate
> someone's work". It cost a real collision: a `/mythos` session audited
> `subsystem:mythos-audit-ansible` for about an hour while a sibling held the claim, and
> nothing surfaced until the operator asked. The delta keys on `claims.first_claimed_at`, not
> `created_at` — `created_at` is reset by every same-session re-claim, and
> claim-renewal hooks re-touch `created_at` on a sliding TTL, so a `created_at`-keyed
> delta would re-announce the same lane forever. Dead/expired holders are filtered so a ghost
> lease is never announced.

```bash
if [[ -f "$HOME/.gaius/concord-hooks-enabled" ]]; then
    DELTA=$(timeout 4 gaius concord brief --scope prompt --session "$SESSION_ID" 2>/dev/null)
fi
```

**PreToolUse (warn-only)** — if a session is about to run a mutating command on a
resource another live session has claimed, surface a warning. Query the DB directly
(fast path, no CLI startup); render **structured fields only** (resource, holder, age)
— never the peer's free-text note. Burn in as warn-only and review the log for false
positives before ever promoting to a hard block.

**Remote federation (optional)** — `gaius concord sync` dual-writes a heartbeat and
findings to any server implementing the same `/concord/heartbeat` + `/concord/finding`
contract, and pulls sibling findings back. Configure `concord.base_url` (and optionally
`concord.api_key`) in `~/.gaius/config.yaml`. Fail-silent with a short timeout: the
local sidecar stays authoritative, and the remote is *allowed to be dark* — that is the
entire point of the local tier. Rate-limit the call (e.g. once per 60s per session)
from your prompt hook.

---

## Design notes (the bright line)

**Automate awareness. Gate action.** Everything concord does is advisory:

- A claim tells a sibling "this is held" — it never acts on the sibling.
- A finding is an **observation**, never an instruction. Inbound sibling text is
  rendered provenance-tagged ("session X observed …") and is not authorization for
  anything: destructive operations still require the operator's confirmation in the
  session that runs them.
- Peer free-text is never rendered mid-tool-loop — warnings show structured fields
  only. Delivery of sibling context happens at prompt boundaries, where a session
  (and its human) can actually deliberate.

Deliberate non-features, learned the hard way:

- **No claim release on session Stop hooks** — stop-style hooks fire after every
  response, not at session end. Releasing there would drop a lease *between the turns*
  of an ongoing session — exactly when a sibling could grab it. Ghost leases are
  handled structurally instead: pid-death reaping + TTL.
- **No enforcement in the module** — enforcement (even warn-only) lives in hooks the
  operator installs and can kill instantly. The coordination substrate itself never
  says no.
- **No custom presence store** — the coding CLI already maintains a session registry;
  concord reads it rather than duplicating it.

## Storage

One SQLite file, WAL mode: `~/.gaius/concord.db` (override: `GAIUS_CONCORD_DB`).
Tables: `claims` (partial-unique on active resource), `findings`, `pool_tasks`,
`session_cursors`. Sidecar by design — coordination churn never touches your fact
corpus.
