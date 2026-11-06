# Publishing to the Hermes plugin catalog

Target listing: **context-steward 0.4.1** —
`https://github.com/jiangkoumo/hermes-context-steward`, maintainer `jiangkoumo`,
`tier: community`, `category: memory`.

The operator has authorized publishing (public repository + catalog PR). As of this
commit the repository is **prepared but not published**: no remote push, no catalog
PR and no listing exists yet, and this preparation installed or changed nothing
under a user's `~/.hermes`.

## Verified mechanism / 已核实的上架方式

Hermes calls its marketplace the **plugin catalog**. It is a curated directory in
`NousResearch/hermes-agent`, not a self-service package registry. A maintainer
reviews and merges a PR adding `plugin-catalog/<name>.yaml`. Users can then find it
with `hermes plugins catalog` / `hermes plugins search` and install it by name.
The plugin page is generated from that entry and the README at its pinned commit.

Primary sources, inspected in clean upstream checkouts fetched from
`NousResearch/hermes-agent` (no local patches):

- v0.23.0 at `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` — the declared minimum and
  the release this plugin is verified against.
- `main` at `90d6b98f` (fetched 2026-11-06) — verified as well.

- [Catalog README and entry schema](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md)
  (`plugin-catalog/README.md`).
- [Catalog submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
  (`website/docs/developer-guide/plugins/catalog-submission.md`).
- [Context engine plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/context-engine-plugin)
  (`website/docs/developer-guide/context-engine-plugin.md`).
- The actual `hermes_cli/plugin_validate.py`, `hermes_cli/plugins.py` registration
  API, `plugins/context_engine/__init__.py` loader and `agent/context_engine.py`
  contract were also inspected. Local source is the basis for this implementation;
  re-check upstream documents at submission time.

The design reference was verified by reading `ds-bot/README.md` with the GitHub API
(`gh api repos/FeiZhuLulu/DeepSeek-Bot/contents/ds-bot/README.md` with the raw content
Accept header). It describes a single separate model judgment choosing checkpoint
or a new part, retaining recent user words and searchable old parts. No DS Bot code
was copied. Generic web search did not expose those details; the repository README
did.

## Preparation / 提交前准备

1. Public repository: `https://github.com/jiangkoumo/hermes-context-steward`
   (planned). Submit as its owner; drive-by submissions of someone else's plugin
   are not accepted.
2. Clean-release verification is **done** and must be repeated for any later pin
   change. Against clean upstream checkouts of v0.23.0 (`2bd378ed`) and `main`
   (`90d6b98f`), with the clean source first on `PYTHONPATH` and only the host venv
   reused: 86/86 tests pass and `hermes plugins validate` passes with **no
   warnings** (no private imports, no core override, security scan `safe`). The
   plugin does **not** depend on the local checkout's uncommitted patches.
3. Keep `plugin.yaml`'s name, version and `requires_hermes` accurate. The current
   minimum `>=0.23.0` is a SemVer floor, not a calendar date. Do not claim a newer
   minimum than the released Hermes version. Dependency list is empty: the plugin
   uses Python stdlib plus the host's existing modules.
4. Run the offline project checks:

   ```bash
   ~/.hermes/hermes-agent/venv/bin/python scripts/check.py
   ```

   This runs real tests and the actual admission validator with mutable files
   isolated inside the repository. Model calls are fake; SQLite/archive/search
   tests and the isolated registration probe are real.
5. On a clean checkout at the intended publication commit, also run the documented
   catalog CI command in an appropriate isolated validation environment:

   ```bash
   hermes plugins validate /absolute/path/to/hermes-context-steward --install-deps
   ```

   This is a **suggested pre-PR command, not an action performed in this task**. The
   CLI dependency provisioning flag may create an environment; it was deliberately
   not used. This plugin declares no extra dependencies, so the catalog CI run adds
   nothing to install. Inspect every warning as well as failures.
6. Ensure only reviewable source is committed. Exclude `.test-hermes/`, `.tmp/`,
   bytecode, caches, databases, credentials and build output. Keep MIT LICENSE and
   both READMEs in the pinned tree. There is no binary build or package upload.
7. Resolve the internal-helper compatibility review described below. Passing a
   validator does not guarantee catalog acceptance.
8. The repository must be publicly reachable and the catalog `sha` must be the full
   **40-character commit** reviewed at that pin. A branch, tag, abbreviated SHA or
   a moving URL is not a valid catalog pin.

## Catalog entry / 目录条目

The ready-to-copy entry lives at [`catalog/context-steward.yaml`](catalog/context-steward.yaml).
`sha: FULL_SHA` is the only placeholder: replace it with the full 40-character commit
SHA after pushing (branch, tag and short SHAs are rejected by the loader). Fields
follow the upstream `Entry schema`: `name`, `repo`, `sha`, `description`,
`maintainer`, `tier`, `category`, `requires_hermes`, `version`, `readme`,
`known_issues`, `capabilities`.

Root layout needs no `subdir`. The engine is registered with
`ctx.register_context_engine`; it registers no tools, hooks or middleware and
requires no plugin-specific environment variable. Provider credentials are owned
by Hermes's auxiliary channel, not declared as this plugin's keys.

## PR and review / 上架步骤

1. In a separate, authorized checkout/fork of `NousResearch/hermes-agent`, add only
   `plugin-catalog/context-steward.yaml`, copied from `catalog/context-steward.yaml`
   with `FULL_SHA` replaced by the reviewed pin.
2. Open the catalog PR, describing behavior, tests, exact compatibility baseline
   and the disclosures below. Only Nous maintainers assign `official` or
   `verified`; use `community`.
3. Catalog CI clones the plugin repository at that exact SHA and validates it.
   Fix every failure and explain warnings. A maintainer reviews the pinned source
   and decides whether to merge. There is no separate marketplace upload form.
4. After acceptance, the generated page is
   `https://hermes-agent.nousresearch.com/docs/plugins/context-steward` and users
   can install by catalog name. That URL describes the expected future page,
   not a current listing.
5. Updates require a **new SHA-bump PR**, reviewed over the old-to-new commit
   range. Update the manifest/catalog version together. Users receive reviewed
   updates through `hermes plugins update context-steward`. Never add a
   self-updater or fetch executable plugin code at runtime.

## Review disclosures / 需向审阅者披露

- Every handoff carrier (checkpoint, fresh, and the mechanical fallback) ends with a
  plugin-owned hint telling the model to call the built-in `session_search` tool
  when a needed detail is not in the handoff, instead of guessing. It is text only:
  no extra network egress, no additional LLM call, and it reads the session history
  through Hermes's own documented tool.
- Pure context-engine registration built on documented plugin surfaces only:
  `agent.context_engine` (the engine contract), `agent.plugin_llm` (the host-owned
  LLM facade) and `hermes_cli.config` (settings). No Hermes internal module is
  imported, no core function is rebound, and the admission validator reports **no
  warnings** — including no private-import warning.
- One judgment call per compaction, through the host LLM facade. Routing, auth,
  timeouts, retries and fallbacks stay host-owned; no credentials are read or
  stored. By default the call runs on the session's active model. Routing it
  through the built-in `auxiliary.compression` slot is opt-in:
  `plugins.entries.context-steward.llm.allow_task_override: true`. This fail-closed
  gate is Hermes's own plugin-LLM policy, not the plugin's.
- Per-phase models are optional (`context.steward.verdict` / `.summary`: provider,
  model). They are passed to the same host facade, so Hermes still resolves and
  authenticates them; a different provider/model pair for the two phases makes two
  calls (small model decides, the other writes the handoff). The plugin never builds
  an HTTP client and never reads a key. Two consequences are documented in the
  README and must be disclosed to reviewers: (a) the operator must also grant
  `allow_provider_override` / `allow_model_override` (with the allowlists), and
  (b) `base_url` / `api_key_env` cannot be honored by the public facade — they log a
  warning and are ignored in favour of declaring a custom provider in Hermes config.
  Structural config errors raise at load; unknown or ungranted routes log the exact
  key to add and fall back to the next permitted route.
- Egress redaction is implemented inside the plugin (the host redactor is not a
  public module). It over-redacts: PEM blocks, known key prefixes, bearer tokens,
  credential-shaped assignments and CLI flags, URL userinfo and credential query
  parameters. The host redactor would be stronger; see the API proposals below.
- Reads settings through Hermes's config API; no direct credential-store/file
  reads, keychain access, shell execution, background service, telemetry or
  plugin-owned persistent store. The host handles transcript archive/commit.
- User messages are retained verbatim, separate from the generated handoff.
  Arbitrary unlabeled or transformed secrets cannot be perfectly recognized by
  shape-based redaction. Successful handoffs are lossy; old records remain in
  Hermes's searchable archive.
- Deliberate behaviour differences from the built-in compressor: no historical
  image-to-placeholder rewrite, no built-in proactive tool-result pruning, no
  second-model summary retry (a failed judgment becomes a mechanical checkpoint),
  and the host's unlabelled continuation nudges can occupy a retained slot. Tool
  calls/results are kept paired and orphans dropped, and no completed work is ever
  invented.
- Compatibility text, not an import: the persisted summary carrier starts with
  Hermes's long-lived `[CONTEXT SUMMARY]:` prefix and ends with the host's summary
  end marker, both copied as literals and asserted equal to the host constants by
  the test suite. If upstream changes either string, this plugin needs a matching
  update — see the API proposals below.
- Automated tests do not establish live model judgment quality. Two live runs on an
  isolated local test bot (v0.23.0 checkout, local model route) verified compaction
  and the archive hint end to end: after compaction, a question answerable only from
  the archived tool output made the model call `session_search` and answer exactly.
  That is one model in one environment, not a guarantee for every model.

## Optional upstream API proposals / 可选的上游接口提案

None of these are filed; they are the public seams that would remove the remaining
duplication and let this plugin (and future ones) rely fully on documented API.

1. **A public summary-carrier helper.** `SUMMARY_PREFIX` / the end marker live in
   `agent.context_compressor`; a context engine that writes its own handoff must
   duplicate the two literals to stay recognisable after a restart. A public
   builder (prefix + marker + metadata key) would remove that coupling.
2. **A public redactor.** `agent.redact.redact_sensitive_text` is internal, so
   plugins that cross an egress boundary re-implement a weaker version.
3. **A plugin-LLM factory usable without a `PluginContext`.** Directory-discovered
   context engines receive a minimal context without `.llm`; constructing
   `PluginLlm(plugin_id=...)` directly works but its own docstring says plugins
   should not do that. A `make_plugin_llm(plugin_id)` factory (or `.llm` on the
   context-engine context) would make the documented path available to every
   engine.
4. **A public live-config notification for external engines.**
   `tui_gateway/session_compression.py::_apply_live_compression_config` currently
   assumes every engine has the built-in compressor's fields and private methods.
   It should dispatch to the external engine's public `update_model` or an
   `on_config_change` hook instead of writing its internals. Version 0.4.1 supplies
   two same-named, plugin-owned compatibility methods and threshold/window/cache
   properties solely because that writer otherwise raises or leaves a stale
   trigger. They import no private symbols. On clearing a live context pin the
   plugin restores the last public `update_model` window; resolving new model
   metadata should remain a host responsibility.

## History visibility after a compaction (investigated 2026-11-06)

Reported symptom: after a checkpoint, Desktop and a mobile client showed only the
new tail, not the earlier conversation. Investigated on a **read-only copy** of the
production `layerfs` state.db (session 2026-11-05 "Bot Chat"):

- Rows: 25 active, **511 archived with compacted=1**, 489 inactive with compacted=0.
- Host read paths disagree: `get_messages_as_conversation(include_compacted=False)`
  returns **25** rows, with `include_compacted=True` **524**.
- 486 of the 489 compacted=0 rows are byte-identical duplicates of a row that is
  active or compacted=1; only 3 have no twin. They are Hermes's rewind-style
  archives of carried-tail originals (intentionally hidden from display so a
  carried message is not rendered twice), not lost history.
- Plugin conformance: archived turns committed through the host are compacted=1
  with `display_order` and `message_uid` populated; the handoff carrier is a single
  `_compressed_summary` row recognised by the host summary detector
  (`[CONTEXT SUMMARY]:`). The test suite asserts this display parity end to end.
- The live gateway path already opts in:
  `tui_gateway/session_registry.py::_live_visible_history` reads
  `include_compacted=True, include_ancestors=True`.
- The REST endpoint `GET /api/sessions/{id}/messages` defaults
  `include_compacted=False` (`hermes_cli/web_routers/sessions.py`). Desktop's
  initial load (`getSessionMessages`) calls it without the flag, while its paging
  calls (`getLatestSessionMessages` / `getOlderSessionMessages`) do pass it.

**Conclusion: not a plugin defect.** The conversation is stored in the shape the
built-in compressor produces and is returned as soon as a caller asks for compacted
history. Upstream suggestions (not filed, no core changes made):

1. Default `include_compacted` to true on `GET /api/sessions/{id}/messages`, or make
   the default mean "the user's conversation": archive-vs-active is an
   implementation detail, not a UI distinction.
2. Have Desktop's `getSessionMessages` pass `include_compacted=true` like its paging
   siblings (and have mobile clients do the same).
3. Document the `active=0, compacted=0` rewind rows so third-party clients know they
   are duplicates and must stay hidden.

## Current status / 当前状态

Ready for the operator to publish. Nothing below has been done yet —

- Not pushed: `https://github.com/jiangkoumo/hermes-context-steward` is the planned
  remote; no repository exists to clone yet, so the catalog `sha` is still
  `FULL_SHA`.
- No catalog PR has been opened against `NousResearch/hermes-agent`.
- Nothing from this preparation was installed into or enabled on a user's profile;
  the four production profiles still run the previously installed plugin copy.
- Verification done: 86/86 tests and `hermes plugins validate` pass with zero
  warnings against clean upstream v0.23.0 (`2bd378ed`) and clean `main`
  (`90d6b98f`), and the live test bot run passed for the 0.3.0 code.
- Remaining before merge: publish + pin the SHA, rerun
  `hermes plugins validate … --install-deps` at that pin, then submit the entry for
  maintainer review. Admission is not automatic. Likely review questions are the
  compatibility literals and the opt-in aux routing, both disclosed above.
