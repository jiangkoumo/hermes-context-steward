# Changelog

## 0.4.0 — 2026-11-06

- **Choose the compression model.** `context.steward.verdict` and
  `context.steward.summary` accept `provider` / `model` (plus `base_url` /
  `api_key_env` for symmetry, which the public facade cannot honor and logs as
  ignored). One block drives the single combined call; two different blocks switch to
  a two-call flow (cheap verdict model, separate handoff writer). Unset keeps
  `auxiliary.compression`.
- Both calls go through the same host-owned plugin LLM facade: Hermes resolves,
  authenticates and retries; the plugin still has no HTTP client and no key access.
- Config validation: malformed blocks (unknown key, provider without model, wrong
  type, bad `api_key_env`/`base_url`) raise at load; an unknown provider or an
  ungranted route logs the exact key to add and falls back to the next permitted
  route.

## 0.3.0 — 2026-11-06

- **No internal Hermes imports.** The engine no longer subclasses or imports the
  built-in `ContextCompressor`; it implements the documented `ContextEngine`
  contract itself and talks to the model through the documented plugin LLM facade
  (`agent.plugin_llm`, i.e. `ctx.llm`). `hermes plugins validate` now passes with
  **zero warnings** (previously: "private core imports").
- **Opt-in aux routing.** The judgment runs on the session's active model by
  default. `plugins.entries.context-steward.llm.allow_task_override: true` routes it
  through `auxiliary.compression`; `verdict_model` needs `allow_model_override`.
- **Self-contained safeguards** replace the parent class: own token estimate,
  own egress redaction, own tool-pair sanitisation, own cooldown/backoff and stale
  attempt guard. A failed or unusable judgment now degrades to a mechanical
  checkpoint (archived old turns plus a pointer to `session_search`) that keeps the
  recent user turns and the protected tail verbatim.
- Handoff formatting uses Hermes's long-lived `[CONTEXT SUMMARY]:` prefix and end
  marker as compatibility literals (asserted equal to the host constants by tests).
- Verified on clean upstream v0.23.0 (`2bd378ed`) and clean `main` (`90d6b98f`):
  45/45 tests and a warning-free validation; live test-bot compaction + archive
  recall passed.

## 0.2.0 — 2026-11-06

- Every handoff carrier (`checkpoint`, `fresh`, and the parent-compressor fallback) now
  ends with one plugin-owned hint: earlier turns are archived in the session history and
  missing details must be recovered with `session_search` instead of guessed. The hint is
  appended after redaction, never produced by the auxiliary model, and kept to exactly one
  copy across repeated compactions.
- Verified live in an isolated test bot (Hermes v0.23.0 checkout, local model route): after
  compaction, a question whose answer existed only in the archived tool output made the
  model call `session_search` and answer exactly.

## 0.1.0 — 2026-11-06

- Initial context engine: one auxiliary judgment per compaction picking `checkpoint` or
  `fresh`, structured JSON handoff, verbatim retention of the latest human requests and the
  protected tail, loss-preserving failure handling.
- `check_ratio` inherits the profile's `compression.threshold` when unset (no separate
  setting required); the built-in 75% small-window floor is intentionally bypassed.
- Retention no longer depends on presentation metadata: requests delivered with
  `display_kind: internal_notification` (e.g. the API server's diagnostic ingress) stay
  eligible, while known synthetic recovery nudges and summary carriers remain excluded.
