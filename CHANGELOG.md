# Changelog

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
