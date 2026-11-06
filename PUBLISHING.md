# Publishing to the Hermes plugin catalog

Target listing: **context-steward 0.2.0** —
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
   reused: 55/55 tests pass and `hermes plugins validate` passes, with one
   private-imports warning disclosed below. The plugin does **not** depend on the
   local checkout's uncommitted patches.
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

- Every handoff carrier (checkpoint, fresh, and the parent fallback) ends with a
  plugin-owned hint telling the model to call the built-in `session_search` tool
  when a needed detail is not in the handoff, instead of guessing. It is text only:
  no extra network egress, no additional LLM call, and it reads the session history
  through Hermes's own documented tool.
- Pure context-engine registration and subclassing; no core source edits,
  function rebinding, registry mutation or runtime monkey patch.
- Network traffic uses Hermes's existing `auxiliary.compression` route. It sends
  bounded conversation samples, recent messages, previous handoff and optional
  memory, with forced redaction. It inherits Hermes routing/retries/timeouts;
  parent fallback may make further summary calls. No hardcoded credentials.
- Reads settings through Hermes's config API; no direct credential-store/file
  reads, keychain access, shell execution, background service, telemetry or
  plugin-owned persistent store. The host handles transcript archive/commit.
- User messages are retained verbatim, separate from the generated handoff.
  Arbitrary unlabeled or transformed secrets cannot be perfectly recognized by
  shape-based redaction. Successful handoffs are lossy; old records remain in
  Hermes's searchable archive.
- The real validator reports `security scan: safe` and `no core override: pass`,
  but warns about **private core imports**. The documented ContextEngine and
  ContextCompressor extension design still uses internal helper/metadata APIs:
  auxiliary cancellation, compression summary/tail markers, message token
  estimates, redaction, and the stale-attempt gate. The development check runner
  also imports the validator. These imports are compatibility dependencies, not
  monkey patches. The manifest minimum cannot make private APIs stable.
- Catalog rule 9 says public surfaces only. Its validator currently treats
  these imports as warnings, leaving human review necessary. A maintainer may
  require a public helper seam or an upstream API change before admitting this
  plugin. Do not hide the warning, claim unconditional market readiness, or
  bypass review. If requested, open an API proposal in separately authorized
  work; this task changes no Hermes core.
- Automated tests do not establish live model judgment quality. One live run on an
  isolated local test bot (v0.23.0 checkout, local model route) did verify the
  archive hint end to end: after compaction, a question answerable only from the
  archived tool output made the model call `session_search` and answer exactly.
  That is one model and one environment, not a guarantee for every model.

## Current status / 当前状态

Ready for the operator to publish. {"status": "prepared"}: these are true as of
this commit and nothing below has been done yet —

- Not pushed: `https://github.com/jiangkoumo/hermes-context-steward` is the planned
  remote; no repository exists to clone yet, so the catalog `sha` is still
  `FULL_SHA`.
- No catalog PR has been opened against `NousResearch/hermes-agent`.
- Nothing from this preparation was installed into or enabled on a user's profile;
  the four production profiles still run the previously installed plugin copy.
- Verification done: 55/55 tests and `hermes plugins validate` pass against clean
  upstream v0.23.0 (`2bd378ed`) and clean `main` (`90d6b98f`); one warning
  (private core imports) remains and is disclosed above.
- Remaining before merge: publish + pin the SHA, rerun
  `hermes plugins validate … --install-deps` at that pin, then submit the entry for
  maintainer review. Admission is not automatic; the private-import dependency is
  the item most likely to be challenged.
