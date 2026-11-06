"""Hermes context engine entrypoint (register_context_engine)."""

from . import steward


def register(ctx):
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    context = config.get("context")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError("context must be a mapping")
    settings = context.get("steward")
    if settings is None:
        settings = {}
    ctx.register_context_engine(steward.ContextSteward.from_settings(
        settings, compression=config.get("compression"),
    ))
