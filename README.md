# Hermes Context Steward

[简体中文](README.zh-CN.md) · [Publishing](PUBLISHING.md) · [MIT](LICENSE)

A pure Python context engine plugin for Hermes Agent. At a configurable share of the
model window (50% by default), one independent auxiliary call chooses a checkpoint
or a fresh context segment and writes the handoff in the same response. The chat
window stays the same. Hermes archives the old messages; the plugin never deletes
history, changes Hermes core, or starts a service.

Inspired by the [DS Bot context steward](https://github.com/FeiZhuLulu/DeepSeek-Bot/blob/main/ds-bot/README.md).
This is an independent implementation for Hermes, with no DS Bot code dependency.

## Behavior

| Decision | New context |
| --- | --- |
| `checkpoint` | System instructions, structured handoff (goal, completed work, work in progress, files/commands/decisions, todo), the latest N user messages verbatim, and a protected recent tail with its tool calls/results. |
| `fresh` | System instructions, a brief handoff, the latest N user messages verbatim, and the final completed assistant reply so those requests are not replayed. Older assistant/tool context is removed from the active request. |

`check_ratio` inherits the profile's `compression.threshold` when omitted in
`context.steward`, defaulting to `0.5` if neither is set.
A `fresh` decision is downgraded to `checkpoint` if work/todos remain or the last
message is not a completed assistant reply (including an active tool round or an
unanswered user request).

Every handoff carrier — `checkpoint`, `fresh`, and the parent-compressor fallback —
ends with one plugin-owned hint: earlier turns are archived in the session history,
and details the handoff lacks (exact wording, paths, numbers, commands, error text,
earlier tool output) must be recovered with `session_search` instead of guessed.
The plugin appends that hint after redaction; the auxiliary model never produces
it, and repeated compactions keep exactly one copy (prior copies are stripped
before re-appending, and it is not fed back as the previous handoff). The default is to preserve 3 user messages, including spacing and newlines.
User requests arriving under diagnostic ingress or internal presentation tags
remain fully eligible as user messages; known synthetic recovery nudges and
summary-only carriers are still excluded. Synthetic handoffs are not counted as human messages.

The engine invokes Hermes's compression auxiliary channel once per successful
judgment, reusing routing, credentials, timeouts, response validation and host route
pins. Provider-level retries/fallbacks may still happen inside Hermes. No provider,
endpoint or key is hardcoded. The judgment reads bounded samples of older records
and the most recent 24 messages, plus the previous handoff and optional memory.
It never sends a million-token transcript to the auxiliary model. After a process
restart or session resume, the previous handoff is recovered separately from the
persisted summary carrier, without ordinary record sampling or truncation. An
oversized legacy handoff falls back to the parent instead of being silently cut.

The model must return structured JSON:

```json
{
  "mode": "checkpoint",
  "handoff": {
    "goal": "Ship the fix",
    "completed": ["Read src/app.py"],
    "in_progress": ["Checking the failing test"],
    "key_context": ["src/app.py; python -m unittest; keep the API stable"],
    "todo": ["Fix the assertion and rerun tests"]
  }
}
```

An invalid mode with a valid handoff becomes `checkpoint`. Malformed JSON or an
unusable handoff invokes the parent compressor's checkpoint path; an auxiliary
exception does the same. That path can make additional summary calls. Its
`abort_on_summary_failure=True` prevents a failed summary from replacing history
with a static placeholder. Missing required user quotes, failure in the parent,
or a replacement that does not shrink context keeps the original messages.

## Compatibility

Tested against the local Hermes v0.23.0 checkout at
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` on macOS, with the checkout's Python venv.
The checkout includes local modifications; this is not a claim of testing every
clean v0.23.0 distribution. `requires_hermes: ">=0.23.0"` is a minimum, not a promise
that future internal compressor helpers stay compatible. Revalidate on upgrades.
There are no extra Python dependencies.

## Install and activate (operator actions)

The following are instructions only; development and tests do not install or
activate the plugin. Place this repository's contents, including `__init__.py`,
`steward.py` and `plugin.yaml`, together at:

```text
$HERMES_HOME/plugins/context-steward/
```

The default is `~/.hermes/plugins/context-steward/`. The directory name matters:
Hermes looks up `context.engine` by directory name. Keep the Python module and
manifest at that directory's root. No pip install is needed.

Merge this into the active profile's `config.yaml` (do not replace existing keys):

```yaml
context:
  engine: context-steward
  steward:
    check_ratio: 0.5
    keep_user_messages: 3
    verdict_model: ""  # inherit auxiliary.compression's model/provider
```

Directory context engines do **not** require a `plugins.enabled` entry. Only one
context engine can be selected. Hermes loads the engine on the next normal agent
initialization; an already-running agent can retain its previous engine.
To switch back, set `context.engine: compressor`.

| Setting (`context.steward`) | Default | Meaning |
| --- | --- | --- |
| `check_ratio` | `0.5` | Finite number strictly between 0 and 1; fraction of the full model context window. No built-in 75% small-window floor. |
| `keep_user_messages` | `3` | Positive integer; number of latest human user messages kept verbatim in both modes. |
| `verdict_model` | `""` | Optional model name on the existing compression auxiliary route. Change its provider/auth through Hermes's `auxiliary.compression` settings. |

Invalid settings fail plugin loading visibly rather than silently taking different
values. `compression.model_thresholds` is intentionally ignored: the steward owns
its check line. A supplied output-token reservation caps the threshold below the
usable input window. Hermes's `compression.enabled` still controls automatic
compression. Manual `/compress` also uses this engine and respects a focus topic.
Keep background `compression.prepare_ahead` and micro-compaction disabled (the
Hermes defaults); those inherited features do not run the steward judgment.

## Privacy and storage

- Network: recent and sampled older conversation text is sent through Hermes's
  configured compression auxiliary route. A model override uses that same route.
- The plugin reads its settings through Hermes's config API. It does not read
  `.env`, `auth.json`, keychains, external credential stores or arbitrary files.
- Model input and generated handoffs are force-redacted with Hermes's redactor;
  credential assignments receive an additional conservative scrub. User messages
  retained verbatim are separate from the generated handoff and may contain
  sensitive text the user originally supplied. The plugin does not rewrite them.
- Redaction recognizes credential shapes; an unlabeled arbitrary secret or a
  model transformation of one cannot be guaranteed detectable. Avoid pasting
  credentials into chats. The plugin's own warnings contain no verdict or provider error text; inherited parent diagnostics follow Hermes's logging policy.
- No plugin database, telemetry, shell commands, background jobs, or services.
  Hermes owns the normal session archive and search. The tests exercise its real
  SQLite archive API and FTS search using a throwaway database inside this repo.

## Test and validate

From this repository, using the existing Mac Hermes venv:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/check.py
```

For a Hermes checkout elsewhere:

```bash
/path/to/hermes/venv/bin/python scripts/check.py --hermes-root /path/to/hermes
```

The runner performs unit tests and the real `validate_plugin_dir` admission checks,
including the isolated registration probe, security scan and no-core-override
check. It disables bytecode and lazy dependency installation and keeps mutable
Hermes state and temporary files under `.test-hermes/` and `.tmp/` in this repo.
It installs no dependencies, changes no user configuration and starts no service.
`--validate-only` runs just admission validation.

**What is real:** Python tests execute, the actual Hermes ABC/compressor/auxiliary
adapter/loader are imported, and SQLite archive + FTS are exercised. **What is
fake:** auxiliary model responses/network dispatch; some targeted fallback tests
stub the parent to prove delegation or failure behavior. A separate test runs
the real parent with a failing fake auxiliary call. No live provider/bot test or
paid model call is claimed.

The official validator passes but warns about imports of compressor internals.
Those are subclass implementation dependencies, not runtime monkey patches. The
warning must be disclosed for catalog review; see [PUBLISHING.md](PUBLISHING.md).

## Limits

This is lossy compaction. Bounded sampling can miss older facts; search the session
archive for exact details. Prompt token usage is inherited from Hermes, and rough
estimates are used before real usage arrives and to reject growing candidates.
Very large recent user messages or an active tool round can prevent a smaller
replacement; preservation wins over truncation. Images/media receive Hermes's
normal finalization, and a change to a required user's exact content causes the
candidate to be rejected.

`fresh` creates a logical context segment, not a new chat ID, UI tab, or explicit
`/reset`; memory/session lifecycle remains owned by Hermes. The host decides
whether compaction commits in place or rotates a session. Automatic success status
is suppressed by the engine, but host warnings and manual `/compress` output can
still be visible. Compression is checked at Hermes's lifecycle seams, which can
include mid-tool-loop/preflight calls rather than only completed turns.

The implementation intentionally subclasses Hermes compressor helpers for token
tracking, protected tail/tool pairing, serialization, response validation and
finalization. It must track changes to those helpers. Marketplace publication
still requires a public repository, an exact commit pin and maintainer approval;
this repository has not been published or submitted.
