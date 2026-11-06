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

The engine makes its one judgment call through Hermes's plugin LLM facade
(`ctx.llm`, or the same `agent.plugin_llm` object when a directory-installed engine
has no plugin context). Provider choice, credentials, timeouts and retries stay
host-owned. By default the call runs on the session's active model; to route it
through the cheaper `auxiliary.compression` slot instead, grant the task override:

```yaml
plugins:
  entries:
    context-steward:
      llm:
        allow_task_override: true   # judgment uses auxiliary.compression
```

`verdict_model` is only applied when `allow_model_override: true` is granted too;
otherwise the host's own route wins. Nothing is hardcoded. The judgment reads
bounded samples of older records and the most recent 24 messages, plus the previous
handoff and optional memory; it never sends a million-token transcript. After a
restart or a resumed session the previous handoff is recovered from the persisted
carrier, without ordinary record sampling or truncation.

## Choosing the compression model

Two optional blocks set the model per phase. Leave them out and Hermes's
`auxiliary.compression` route is used exactly as before.

```yaml
context:
  engine: context-steward
  steward:
    verdict:                    # decides checkpoint vs fresh
      provider: magpie
      model: opendesign/deepseek-v4.1-flash
    summary:                    # writes the handoff
      provider: magpie
      model: opendesign/glm-5.3-flash
```

- **One block** (either one) — it is used for the single combined judgment call.
- **Both blocks, same provider+model** — still one call.
- **Both blocks, different provider or model** — two calls: a small model decides the
  mode, the second writes the handoff.
- A plain string is accepted as shorthand for a model name:
  `verdict_model: some-model` (legacy) or `verdict: some-model`.

The plugin does **not** call providers itself: both models are resolved, authenticated
and retried by Hermes. That means the operator must also grant the override, and the
names must exist in the profile's own provider configuration:

```yaml
plugins:
  entries:
    context-steward:
      llm:
        allow_provider_override: true
        allowed_providers: [magpie]
        allow_model_override: true
        allowed_models: [opendesign/deepseek-v4.1-flash, opendesign/glm-5.3-flash]
```

`base_url` and `api_key_env` exist for symmetry with other plugin settings but are
**not supported** by Hermes's public plugin-LLM API: it accepts a provider *name*, not
an endpoint or a key. Setting them logs a warning and they are ignored. To use your
own endpoint, declare it once as a custom provider in `config.yaml` (it owns the URL
and credential) and reference it by name.

Bad configuration is never silent: a malformed block (unknown key, pre-set
`provider` with an empty `model`, a non-string value, a bad `api_key_env` or
`base_url`) raises at load, so Hermes logs the reason and falls back to the built-in
compressor. An unknown provider, or a route the operator has not granted, logs a
warning naming the exact key to add and falls back to the next permitted route
(aux `compression`, then the session model).

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

An invalid mode with a valid handoff becomes `checkpoint`. Malformed JSON, an empty
or unusable handoff, a timeout or any transport error degrades to a **mechanical
checkpoint**: Hermes still archives the old turns, the retained user requests and
the protected tail stay verbatim, and the handoff states only what was recovered
deterministically (the pending request plus paths/commands/errors lifted from the
transcript). Nothing is invented and nothing is dropped silently. If even that
cannot shrink the context, or if the retained user text or tail would change, the
original transcript is returned unchanged.

## Compatibility

Verified against **clean upstream checkouts** (no local patches) of
`NousResearch/hermes-agent`: v0.23.0 at `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`
(the declared minimum) and `main` at `90d6b98f` (2026-11-06). In both, with the clean
source first on `PYTHONPATH` and only the host venv reused, all 66 tests pass and
`hermes plugins validate` passes with **no warnings** — the plugin imports only
documented modules (`agent.context_engine`, `agent.plugin_llm`, `hermes_cli.config`)
and does not subclass or import Hermes's internal compressor.

A live end-to-end run on an isolated test bot (v0.23.0 checkout, local model route)
confirmed the archive hint: after compaction, a question answerable only from the
archived tool output made the model call `session_search` and answer exactly. That
is one model in one environment, not a guarantee for every model.

`requires_hermes: ">=0.23.0"` is a minimum, not a promise that future plugin
surfaces stay source-compatible; revalidate on upgrades. There are no extra
Python dependencies.

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
    verdict_model: ""  # optional; needs the allow_model_override grant below
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

Verification against a clean upstream checkout (the catalog CI shape):

```bash
/path/to/hermes/venv/bin/python scripts/check.py --hermes-root /path/to/clean/hermes-agent
```

That is how the results above were produced for `2bd378ed` and `90d6b98f`.

`scripts/check.py` runs the unit tests and then the real
`hermes plugins validate` as a subprocess (it never imports core modules itself),
including the isolated registration probe, security scan and no-core-override
check. It disables bytecode and lazy dependency installation and keeps mutable
Hermes state and temporary files under `.test-hermes/` and `.tmp/` in this repo.
It installs no dependencies, changes no user configuration and starts no service.
`--validate-only` runs just admission validation.

**What is real:** Python tests execute, the actual Hermes ContextEngine contract,
plugin LLM facade, config loader, directory loader and SQLite archive + FTS are
exercised, and the admission validator runs end to end. **What is fake:** the
judgment model's responses (a recorded stand-in for `ctx.llm`); no provider call is
made from this suite. The live bot check was a separate manual run.

The validator reports **no warnings**: the runtime imports only documented modules
and does not touch Hermes's internal compressor.

`scripts/slimtest_live.py` is an opt-in maintainer harness that talks to a
throwaway local gateway (`HERMES_SLIMTEST_HOME`, `HERMES_SLIMTEST_URL`); it is not
part of the plugin runtime and is never imported by it.

## Limits

This is lossy compaction. Bounded sampling can miss older facts; search the session
archive for exact details. Prompt token usage comes from the host's real usage; a
rough character-based estimate is used only for preflight and for rejecting
growing candidates. Very large recent user messages or an active tool round can
prevent a smaller replacement; preservation wins over truncation. A change to a
required user's exact text or to the protected tail rejects the candidate.

Compared with Hermes's built-in compressor this engine deliberately does less:
it does not rewrite historical images into placeholders, does not run the built-in
proactive tool-result pruning, and does not retry a failed summary on a second
model (a failed judgment becomes a mechanical checkpoint instead). It does keep
tool calls and results paired and drops orphans, and it never invents completed
work. A host-injected continuation nudge that arrives as an ordinary `user` row can
occupy one of the retained user slots; such rows are tiny and the original is still
archived.

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
