# MCP Probe-Failure Quarantine

Status: implemented (this PR)
Owners: `src/kiro_crew/mcp_quarantine.py` (the verdict store),
`rebuild_agent_config` in `src/kiro_crew/agent.py` (the mount decision),
`dashboard/handlers/mcp.py` (recording, annotation, release)

## 1. Problem

A probe verdict was display-only. `probe_server` could time out on the same
server eight consecutive times and `rebuild_agent_config` would still write its
`@ref` into every agent's `tools`, so every new session spawned it again.

The policy was written down as intentional, in `api_mcp_probe`'s docstring —
"probe results don't reset user's previous enable/disable choices" — and the
mount decision honoured exactly one input:

```python
if spec.get("disabled") or alias in _disabled_anywhere:
```

There was no third state. A server was either what the user chose or nothing,
and "we tried this eight times and it never answered" had nowhere to go. The
cost is paid once per session start, forever, with nothing in the product that
converges: on one real host enough re-mounted spinners accumulated to starve the
gateway's event loop into three watchdog hard-exits in a day while the user was
idle.

## 2. Solution overview

Add the third input, as a state of its own rather than a second writer of
`disabled`.

- **`disabled` is the user's**, lives in `~/.kiro/settings/mcp.json`, and
  nothing in this feature writes it.
- **`quarantined` is ours**, lives in `<data home>/mcp-quarantine.json` and in
  the *generated* agent spec — a derived artifact rebuilt from scratch every
  time — so releasing it is one counter delete plus a rebuild.

Keeping them separate is what makes release safe: a quarantine that flipped the
user's key could resurrect a server they had switched off by hand, and they
would have to guess which flag they were looking at to undo it.

## 3. Mechanism

**Counting.** Both probe paths (`_run_mcp_probe` and `POST /api/mcp/probe`) fold
their rows into `record_verdicts` in one write. `error` and `timeout` increment;
`ok` deletes the record outright. Every other status carries no verdict and
leaves the record untouched — `disabled` (never probed), `unknown` / `outdated`
(no fresh result), and `needs_auth`, which is a server working correctly and
asking for a token. Counting `needs_auth` would quarantine every OAuth
connection the user has not signed into yet, and a quarantined server can never
be signed into, so that state would be self-sealing.

Kiro Crew's own managed servers are excluded at this boundary, not at the mount
decision. `kirocrew-core` carries spawn_run, learn_add and the monitor loop, so
quarantining it would remove the tools the product is made of — and since the
mount decision never touches managed names, counting their failures could only
ever produce a badge claiming a server was unmounted when it was not. No record,
no badge, no count.

A counter rather than a single failure because one probe failure is routinely
transient (cold npm cache, a laptop that just woke, a registry blip). The claim
being made is "consistently unreachable".

**Deciding.** Crossing `agent.mcp_quarantine_after_failures` (default 3) stamps
`quarantined_at`. `record_verdicts` returns the names whose MOUNT STATE changed
on that call — in BOTH directions, since a recovery has to trigger a rebuild too:
its record is deleted so nothing calls it quarantined any more, but the `@ref`
and the `mcpServers` entry do not come back until something regenerates the
config. A recovery below the threshold is deliberately not reported; that server
never stopped being mounted, and reporting it would rebuild on every probe round
in which anything carried one stale failure.

**Unmounting.** Applied as its own pass over `valid_servers` — the emitted mount
map — rather than inside `rebuild_agent_config`'s scope walk, so the decision runs
against the complete set rather than a walk over config scopes.

Two classes are excluded, and both sides consult one function,
`quarantine_eligible_aliases`, so the recording boundary and the mount decision
cannot drift into disagreeing about which servers are in play:

- **Kiro Crew's own managed servers**, for the reason above.
- **Servers configured ONLY in the generated agent config** (`kiro-cli mcp add
  --agent kirocrew`, or a hand-edit). There the agent config is the sole
  persisted copy of the user's configuration, so dropping the entry would destroy
  it, with no source for discovery or a later release to restore from. Stamping
  `disabled` instead is no better — `list_servers` adds such a name to
  `disabled_in_agent` and then introduces it from nowhere, so the row disappears
  along with the badge and the release control. Neither lever is safe for that
  scope, so the server is left mounted. **Known limitation:** a broken agent-only
  server keeps costing a spawn per session. That is a smaller harm than deleting
  configuration the user cannot get back.

- **A contested alias.** A shared scope's slashed key (`npm:@x/airbnb`) is emitted
  under its slash-free alias, but `_normalize_mcp_server_keys` moves only slashed
  keys -- so if a slash-free server of that name already occupies the alias, the
  shared one is preserved at `<alias>-2` and the bare alias still belongs to the
  other server. Treating the alias as eligible because a shared scope derives it
  would drop the wrong entry, and when the occupant is agent-only that destroys
  its sole copy. An alias derived from a slashed shared key is therefore eligible
  only while no slash-free key anywhere claims it.

For an eligible server the pass removes the `@ref` from `tools` and
`allowedTools` **and** drops the `mcpServers` entry. Both halves matter: the ref is
what EXPOSES the server, but the entry is what makes kiro-cli SPAWN it, and the
per-session spawn is the cost this exists to stop paying. Dropped rather than
stamped `disabled: true` — see §7.

**Releasing.** `POST /api/mcp/quarantine/clear` deletes the record and rebuilds
in the same request. It clears the COUNTER as well as the flag: releasing a
server one failure short of re-quarantine would make the button look broken.

If the rebuild fails, the release is ROLLED BACK. `clear` hands the removed
record to the caller for exactly this: without the rollback a half-done release
is unrecoverable from the UI, because the store says released — so the badge and
the Remount control both disappear on the next poll — while the emitted config
still omits the server, and the control the user would retry with is the one that
just vanished. Restoring the record keeps the server visibly quarantined, which
is also the truth. If the rollback write ALSO fails the response says so with its
own code rather than implying the state is intact.

**Off switch.** `0` disables the mechanism, and `quarantined_names()` re-reads
the threshold on every call, so turning it off also releases what it already
caught rather than leaving servers unmounted with no surface that explains why.

**Fail-open.** An unreadable or malformed store reads as no records. This module
can only ever REMOVE a server from the agent config, so failing closed would let
one bad byte on disk unmount the whole fleet.

Failing open alone is not enough, though, so `reset_unreadable_store` runs at the head
of each probe round. An unreadable store silently empties the quarantined set, so
the badge and the Remount control vanish while the generated config — written when
the store was still readable — goes on omitting the server. Nothing would
reconcile that on its own: `record_verdicts` reads the same empty view, so a
failing server counts up from zero, crosses no threshold, and triggers no rebuild.
Repairing at the point of detection, and forcing one rebuild, keeps the cost
proportionate — rebuilding after every probe would regenerate the agent config on a
timer forever to cover a rare corruption.

## 4. Surface

The MCP table (`website/src/pages/overview/McpTab.tsx`) shows a second badge
beside the probe status, not instead of it: `error` is still the true reading and
the error detail under it is keyed on that status, so the quarantine badge says
what was DONE about it — a different fact. Its tooltip carries the failure count
and states that the user's enable setting is unchanged. A `Remount` action
appears on quarantined rows only, and the row is repainted from a refetch rather
than optimistically, so the badge clears only if the backend really remounted the
server.

Wire shape: `probeFailures` and `quarantined` are added to a probe row only when
that server has failures on file, so a healthy fleet's response is unchanged.
Annotation happens at response time, not in the cached rows, so a release is
visible on the next poll instead of waiting for a re-probe.

## 5. Scope boundary: already-running sessions

Quarantine reaches sessions that start after it engages. It deliberately does
not tear down live sessions or the warm pool.

A running kiro-cli read its agent spec at spawn and never re-reads it, so
propagating into it means killing the process mid-turn. Losing a user's in-flight
work to reclaim a core is the worse end of that trade, and the dashboard already
owns the explicit path for when the user wants it: `POST /api/sessions/restart`
drains active sessions and the warm pool, and the next message cold-starts on the
new spec.

The practical consequence, worth stating plainly: a long-lived session keeps
mounting a broken server for as long as it lives.

## 6. Audit

A quarantine gets its own SEL operation, `mcp_auto_quarantined`, rather than
folding into the existing `mcp_tools_removed` line — an operator reading
"removed (disabled)" would conclude a human did it, and this is the one removal
reason nobody chose. Release records `mcp_quarantine_released`.

## 7. Why absence, not `disabled: true`

The first implementation stamped `disabled: true` on the emitted `mcpServers`
entry, reasoning that a user disable already arrives carrying that key so the
quarantine should too. That is self-defeating. `list_servers` builds
`disabled_in_agent` from the generated agent config and then refuses to introduce
those names from any other scope, so the stamp suppressed the server's own
dashboard row — taking the quarantine badge and the only control that releases it
with it, and leaving a server unmounted with no surface anywhere that said why.

Absence carries the same weight to kiro-cli (it cannot spawn what is not in the
map) and leaves the row visible, which is where the state has to be explained.
Pinned by a test asserting the entry is absent rather than present-and-disabled.
