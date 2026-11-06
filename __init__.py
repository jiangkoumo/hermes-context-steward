"""Hermes context engine entrypoint (`context.engine: context-steward`).

Registers the engine through the documented plugin surface. Only public Hermes
modules are imported here and in :mod:`steward`: `agent.context_engine` (the engine
contract) and `agent.plugin_llm` (the host-owned LLM facade behind `ctx.llm`).
"""
from . import steward


def register(ctx):
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    context = config.get("context")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError("context must be a mapping")
    settings = context.get("steward")
    if settings is None:
        settings = {}
    # `ctx.llm` exists when the plugin is loaded through the general plugin system;
    # a directory-installed context engine gets a no-op context, so the engine
    # builds the same facade lazily instead.
    ctx.register_context_engine(steward.ContextSteward.from_settings(
        settings, compression=config.get("compression"), llm=getattr(ctx, "llm", None),
    ))
