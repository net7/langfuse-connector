from datetime import datetime
from typing import Dict
from cat.mad_hatter.decorators import hook, plugin
from cat.looking_glass.stray_cat import StrayCat
from cat.log import log
from langfuse import Langfuse
from langchain.docstore.document import Document
from langfuse import propagate_attributes

_langfuse_client: Langfuse | None = None

@hook
def after_cat_bootstrap(cat: StrayCat) -> None:
    """Initializes the Langfuse client when the Cat bootstraps."""
    global _langfuse_client

    if _langfuse_client is not None:
        return  # Already initialized

    try:
        settings = cat.mad_hatter.get_plugin().load_settings()
        if not settings.get("enable_tracing", True):
            log.info("[Langfuse] Tracing disabled by configuration")
            return

        public_key = settings.get("langfuse_public_key")
        secret_key = settings.get("langfuse_secret_key")
        host = settings.get("langfuse_host", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            log.warning("[Langfuse] Missing keys, client not initialized")
            return

        _langfuse_client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        log.info(f"[Langfuse] Client initialized on {host}")
    except Exception as e:
        log.error(f"[Langfuse] Error during client initialization: {e}")


def _get_client() -> Langfuse | None:
    """Returns the singleton Langfuse client."""
    return _langfuse_client

@hook
def before_cat_sends_message(message: dict, cat: StrayCat) -> dict:
    """
    Updates and flushes the main trace before sending the message.
    """
    langfuse = _get_client()

    if not langfuse:
        log.debug("[Langfuse] Client not available, skipping trace update.")
        return message

    user_input = getattr(cat.working_memory, "user_message_json", {}).get("text", "")
    final_output = getattr(message, "content", "")

    # Update main trace with final input/output
    interactions = getattr(cat.working_memory, "model_interactions", [])

    # Add spans from model_interactions
    try:
        with langfuse.start_as_current_span(name="root-trace", input=user_input) as root_span:
            with propagate_attributes(user_id=cat.user_id):
                cat.working_memory.trace_id = root_span.trace_id

                span_counter = 0
                for interaction in interactions:
                    if getattr(interaction, "model_type", None) == "llm":
                        meta = interaction.model_dump()
                        span_counter += 1
                        with root_span.start_as_current_generation(
                                name=f"LLM Call {span_counter}",
                                input=interaction.prompt,
                                output=interaction.reply,
                        ) as gen:
                            gen.update(
                                output=interaction.reply,
                                usage_details={
                                    "input_tokens": meta.get("input_tokens", 0),
                                    "output_tokens": meta.get("output_tokens", 0)
                                },
                                metadata={
                                    "started_at": meta.get("started_at", 0),
                                    "ended_at": meta.get("ended_at"),
                                    "latency": meta.get("ended_at", 0) - meta.get("started_at", 0)
                                }
                            )
                root_span.update(output=final_output)
    except Exception as e:
        log.error(f"[Langfuse] Error during tracing: {e}")

    return message


@plugin
def deactivated(plugin):
    """Called when the plugin is deactivated."""
    global _langfuse_client
    if _langfuse_client:
        _langfuse_client.flush()
        log.info("[Langfuse] Flush completed on deactivation")
        _langfuse_client = None