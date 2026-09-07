# Design: fix per-request lifespan re-entry (frontmatter_index + retrieval)

**Status: DRAFT — not yet implemented. For review before any code changes.**
**Author:** Claude Sonnet 5, 2026-09-06 night session, investigating the bug diagnosed
the same night while enabling `RETRIEVAL_ENABLED`.

---

## 1. The bug, precisely

`server.py` registers a custom `lifespan()` async context manager with `FastMCP(...,
stateless_http=True, lifespan=lifespan, ...)`. Today, `lifespan()`:

1. Calls `frontmatter_index.start()` (full `rglob` walk of the vault + spawns a
   `watchdog.Observer` — a native FSEvents-backed OS thread on macOS).
2. If `RETRIEVAL_ENABLED`, constructs a fresh `RetrievalStore`/`Embedder`/
   `RetrievalIndexer`, and calls `frontmatter_index.add_change_listener(...)`.
3. `yield`s.
4. On exit, calls `frontmatter_index.stop()` and `retrieval_store.close()`.

**This is wrong under `stateless_http=True`.** Confirmed directly in the installed MCP
SDK (`mcp/server/streamable_http_manager.py`, `_handle_stateless_request`): stateless
mode "creates a completely fresh transport for each request" and calls
`self.app.run(...)` per request; `mcp/server/lowlevel/server.py:657` — `Server.run()`
enters `self.lifespan(self)` fresh every time it's called. So **`lifespan()` re-enters
on every single MCP request**, not once at process boot.

**Live proof, same session:** process `pid=46618` never exited (`runs=1` via
`launchctl print`), yet logged `"Semantic retrieval enabled"` (which only happens once
per `lifespan()` entry) **5 times in 9 minutes** of ordinary traffic.

**Consequences, all confirmed or directly implied by the above:**
- Every request pays a synchronous full-vault re-walk (1–4s on this ~11,300-file vault),
  blocking the event loop.
- `frontmatter_index` is a single module-level global; `add_change_listener()` fires on
  every request, so its `_change_listeners` list **grows by one every request, forever**.
  Real behavioral cost: every actual file-change event fires *N* accumulated listeners
  instead of one → *N* redundant Ollama embedding calls per file edit, scaling with
  request count.
- Each `Observer()` racing a prior one on the same path collides in watchdog's native
  layer (`RuntimeError: Cannot add watch ... it is already scheduled`) — the proximate
  symptom that surfaced this tonight.
- `retrieval_store`/`retrieval_embedder`/`retrieval_indexer` are re-created and the old
  store `.close()`d on every request; under genuinely concurrent requests, one request's
  teardown can close a connection another concurrent request still holds.

## 2. This exact bug has already been found and fixed — twice — elsewhere

This is not a novel problem needing an invented solution. It was diagnosed and fixed
independently in two other places, neither of which this repo (`obsidian-web-mcp`) had
ever merged:

**a) Upstream, `jimprosser/obsidian-web-mcp` commit `13e147f`** (7 Jun 2026, "#28"):
moved `frontmatter_index.start()` to `main()`, made `lifespan()` a no-op `yield {}`,
added an idempotency guard inside `FrontmatterIndex.start()` itself
(`if self._observer is not None: return`), used `atexit.register(frontmatter_index.stop)`
for cleanup. Verified live: "index builds exactly once across multiple MCP requests."

**b) `prouds-mcp` (Jett's own fork) commit `b8c199e`** (3 Aug 2026): same root cause,
found via a **live production hang** — a burst of ~10 concurrent MCP handshakes pegged
the server at 100% CPU and stopped it responding to anything, confirmed with a real
`faulthandler` thread dump. Fixed by moving `frontmatter_index.start()`/`.stop()` into
`main()`, wrapped in `try/finally` around `uvicorn.run()` (not `atexit` — see §3 for why
this repo should follow `prouds-mcp`'s pattern, not upstream's, on that specific point).
Also found and fixed a **second, independent bug** in the same investigation:
`AuditMiddleware.replay()` (an ASGI `receive()` substitute for request-body buffering)
never signaled `http.disconnect`, turning the downstream SSE session handler's
disconnect-detection poll into a CPU-pinning busy loop. **Checked tonight: this second
bug does not exist here** — `obsidian-web-mcp`'s `audit.py` uses a decorator
(`@audited`) around individual tool functions, not an ASGI middleware with a body-replay
shim; grepped the whole `src/` tree for `replay`/`http.disconnect`/`more_body` — no
matches. `auth.py`'s `BearerAuthMiddleware` passes the original `receive` straight
through unmodified. One less thing to fix, confirmed rather than assumed.

`prouds-mcp` also has a real, subprocess-based regression test for exactly this class of
bug: `tests/test_frontmatter_index_lifecycle.py` — spins up a real server subprocess,
opens overlapping MCP handshakes, asserts the server stays responsive. This is the right
verification shape (§5) and I'm adapting it here rather than inventing a new one.

## 3. Item 1 — should `stateless_http` be dropped instead?

**No. Recommendation: keep it, fix the initialization placement instead.**

Investigated, not assumed:
- Grepped the whole codebase, `git log`, and both prior fix commits for anything that
  depends on "fresh transport per request" as a *feature* (session-affinity avoidance,
  horizontal scaling, etc.) — found nothing. This server runs as exactly one process on
  one machine (`launchd`, `KeepAlive=true`), never horizontally scaled, so the typical
  argument *for* statelessness (no server-side session state needed across multiple
  instances) doesn't apply here either way — it's neutral, not a point in favor of
  keeping it, but also not a reason to drop it.
- Both prior fixes (upstream and `prouds-mcp`) chose to **keep** `stateless_http=True`
  and fix the init-placement bug instead of switching modes. Neither considered
  switching modes as the fix, in two independent investigations of the identical root
  cause. That's real signal, not just precedent for its own sake.
- Switching to stateful mode (`stateless_http=False`) is a strictly larger, riskier
  change: it introduces `Mcp-Session-Id` header handling, an event store for
  resumability, and session-lifecycle semantics that don't exist in this codebase today
  — and could change how real, currently-working clients (jarvis-app, claude.ai's
  connector) behave against this server. That's a lot of new surface area to fix a bug
  whose actual cause is "this code runs in the wrong place," not "stateless mode itself
  is wrong for this server."

Conclusion: **stays `stateless_http=True`.** Fix is entirely about *where*
initialization code lives, not the transport mode.

## 4. Item 2 — one-time init design

Adopt `prouds-mcp`'s pattern (`try/finally` in `main()`), not upstream's (`atexit`):
`atexit` handlers can be skipped or run in a surprising order under `SIGKILL`,
multiprocessing, or certain exception paths; a `try/finally` wrapped directly around
`uvicorn.run()` in `main()` guarantees the teardown call happens exactly when this
process's serving loop actually ends, with no dependency on interpreter shutdown timing.
Also add upstream's idempotency guard (`if self._observer is not None: return`) inside
`FrontmatterIndex.start()` as defense-in-depth — cheap, and closes off the failure mode
structurally rather than relying solely on call-site discipline.

```python
# server.py

async def lifespan(server):
    """Deliberately does nothing beyond yielding an empty context.

    stateless_http=True means FastMCP's Server.run() re-enters this on EVERY
    request, not once per process (confirmed 2026-09-06 against the installed
    mcp SDK -- see LIFESPAN-INIT-DESIGN.md). frontmatter_index and retrieval
    are long-lived, server-wide resources with no reason to be tied to a
    per-request session; they start once in main() and stop once there too.
    """
    yield {}


def main():
    ...
    # Frontmatter index + semantic retrieval: started ONCE here, at real
    # process startup, not per-request. See lifespan()'s docstring and
    # LIFESPAN-INIT-DESIGN.md for why this used to live there and the
    # concurrency bugs (fsevents collision, listener-list growth, and a
    # close()-under-concurrency race) that caused, all confirmed 2026-09-06.
    global retrieval_store, retrieval_embedder, retrieval_indexer
    logger.info(f"Starting vault MCP server. Vault: {VAULT_PATH}")
    frontmatter_index.start()
    logger.info(f"Frontmatter index built: {frontmatter_index.file_count} files indexed")

    if config.RETRIEVAL_ENABLED:
        ... # exact same wiring as today, just moved here, executed exactly once
        frontmatter_index.add_change_listener(...)
    else:
        logger.info("Semantic retrieval disabled ...")

    # ... build app, app.add_middleware(...), etc. (unchanged) ...

    try:
        uvicorn.run(app, ...)
    finally:
        frontmatter_index.stop()
        if retrieval_store is not None:
            retrieval_store.close()
        logger.info("Vault MCP server shut down.")
```

`frontmatter_index.py`:
```python
def start(self) -> None:
    """... Idempotent: a second call while already running is a no-op."""
    if self._observer is not None:
        return
    ...
```

## 5. Item 3 — listener-accumulation fix

With retrieval init moved into `main()` (§4), `add_change_listener()` is called exactly
once per process by construction — the unbounded-growth failure mode is eliminated
structurally, not by adding a dedup check on top of a still-broken call site. No separate
dedup logic is proposed: lambdas aren't meaningfully comparable/hashable for a dedup
check to be reliable, and a dedup check would be treating the symptom. Verification (§6)
asserts the list length directly rather than trusting the reasoning alone.

## 6. Item 4 — close()-under-concurrency race

Same resolution as §5: once `retrieval_store`/`retrieval_embedder`/`retrieval_indexer`
are true process-lifetime singletons (assigned once in `main()`, closed once in the
`finally` block after `uvicorn.run()` returns), there is no code path left that can call
`retrieval_store.close()` while a request is in flight — the only close() call is after
`uvicorn.run()` has already returned, meaning no request is being served at all at that
point. No lock is added, because there's no longer a critical section for a lock to
protect. Flagging the alternative for the record: a threading.Lock around store access
was considered and rejected as unnecessary complexity for a race that the redesign
removes entirely, not one it merely narrows. If review disagrees and wants defense in
depth here specifically, that's a one-line addition, not a structural change to this
plan — happy to add it, just wasn't the default given nothing left to race with.

## 7. Verification plan (before this is called done)

Matching prouds-mcp's rigor, not less:

1. **Adapt `tests/test_frontmatter_index_lifecycle.py`** from `prouds-mcp` into this
   repo: real server subprocess, real overlapping MCP handshakes sent concurrently,
   assert the server stays responsive and the "already scheduled" error never appears
   in its log.
2. **Listener-list-length proof**: a test (or the same live subprocess test) that fires
   N real concurrent requests, then asserts `len(frontmatter_index._change_listeners)`
   is still 1 (or 0 pre-retrieval), not N — this is the concrete, non-hand-wavy version
   of "prove the list stops growing" per your instruction.
3. **Real before/after latency measurement**: time N sequential real MCP tool calls
   against the live-restarted service before and after the fix (wall-clock via the same
   MCP client script used tonight's item-3 verification, not a synthetic benchmark) —
   expect the per-call fixed cost of a full frontmatter rewalk to disappear after the
   fix.
4. **Real concurrent-request test against the actual running production service**,
   post-fix, not just the subprocess test in CI: fire several real overlapping
   `vault_search_semantic`/`vault_search` calls at once against
   `https://vault-mcp.wzdmai.com`, confirm no errors, no "already scheduled" in the log,
   correct results from all of them.
5. Full test suite must still pass (all existing + new tests).
6. Restart the live service once with the fix, watch the log for a clean single
   "Semantic retrieval enabled" / "Frontmatter index built" pair per actual restart,
   not per request, over a real observation window (e.g. 15+ minutes of ordinary
   traffic, matching how tonight's bug was originally caught).

## 8. Explicitly out of scope for this fix

- `prouds-mcp`'s bug #2 (`AuditMiddleware.replay()`) — confirmed not present here (§2).
- The pre-existing `origin/main` divergence (24 unmerged upstream commits) — unrelated,
  already tracked separately.
- Any change to `stateless_http` mode itself (§3 — explicitly recommends against).

---

**Requesting review of this design before implementation**, per instruction. Once
approved, implementation is a small, mechanical diff (move code, add one guard, wrap in
try/finally) — the design decision is the part that matters, not the line count.
