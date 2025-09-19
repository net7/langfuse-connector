"""Langfuse Connector Plugin for Cheshire Cat.

This plugin integrates Langfuse with the Cheshire Cat to provide detailed, real-time
tracing and observability for interactions, including standard LLM calls and early
short-circuit replies.

The tracing logic is designed to be robust and simple:
- `agent_prompt_prefix`: injects the Langfuse `CallbackHandler` right before the LLM call.
  The handler automatically creates a trace on LLM execution.
- `agent_fast_reply` (priority=-1): when a plugin short-circuits after recall but before the LLM,
  this hook creates/reuses a trace and records a span for the reply. Since this is the end of the
  interaction, we flush immediately in this path.
- `fast_reply` (priority=-1): when a plugin short-circuits immediately (before recall/agent/LLM),
  this hook creates/reuses a trace and records a span, then flushes immediately. The finalizer does
  not run on this path, so we do not add a trace id to the outgoing message.
- `before_cat_sends_message`: finalizes traces in LLM/agent paths by adding the original user input
  and the final AI output, and adds `message.langfuse_trace_id` for client-side reference. It also
  performs cleanup to keep interactions isolated.
"""

from typing import Dict
from langfuse.callback import CallbackHandler
from langfuse import Langfuse
from cat.mad_hatter.decorators import hook
from cat.log import log
from cat.looking_glass.stray_cat import StrayCat


@hook
def agent_prompt_prefix(prefix: str, cat: StrayCat) -> str:
    """Prepare and inject the Langfuse CallbackHandler right before an LLM call.

    This hook is the main entry point for tracing standard LLM interactions.
    It runs with a low priority number to ensure it executes after other
    plugins might have modified the LLM configuration.

    It initializes the `CallbackHandler` with the project keys, host, and
    user/session details. The handler is then injected into the Cat's LLM
    callbacks list, where it will automatically trace the subsequent execution.

    An `injected` flag is set on the Cat instance to prevent the handler from
    being injected multiple times during the same interaction.
    """
    try:
        settings = cat.mad_hatter.get_plugin().load_settings()

        # Check if tracing is enabled in the settings
        if not settings.get("enable_tracing", True):
            return prefix

        # Avoid re-injecting if already done for this interaction
        if hasattr(cat, "langfuse_handler_injected"):
            return prefix

        if settings.get("langfuse_public_key") and settings.get("langfuse_secret_key"):
            handler = CallbackHandler(
                public_key=settings["langfuse_public_key"],
                secret_key=settings["langfuse_secret_key"],
                host=settings.get("langfuse_host", "https://cloud.langfuse.com"),
                user_id=cat.user_id,
                session_id=cat.user_data.id,
            )
            if not hasattr(cat._llm, "callbacks") or cat._llm.callbacks is None:
                cat._llm.callbacks = []
            cat._llm.callbacks.append(handler)
            cat.langfuse_handler = handler
    except Exception as e:
        log.error(
            f"Failed to initialize Langfuse CallbackHandler in agent_prompt_prefix: {e}"
        )
    finally:
        # Mark as injected to prevent this from running again in the same interaction
        cat.langfuse_handler_injected = True

    return prefix

@hook(priority=-1) # Run this hook after other agent_fast_reply hooks
def agent_fast_reply(fast_reply: Dict, cat: StrayCat) -> Dict:
    """Trace replies returned by `agent_fast_reply` (post-recall, pre-LLM).

    Priority is set to -1 to execute after other agent_fast_reply hooks. When a plugin returns
    a direct reply here, we create/reuse a trace and record a span. As this path ends the
    interaction, we flush immediately. The `before_cat_sends_message` finalizer may still run
    in some flows and update the trace, but it does not add a trace id in this hook.
    """
    try:
        settings = cat.mad_hatter.get_plugin().load_settings()

        if not settings.get("enable_tracing", True):
            return fast_reply

        # Only proceed if there's a fast_reply output to trace
        if isinstance(fast_reply, dict) and "output" in fast_reply:
            user_message = cat.working_memory.get("user_message_json", {})
            input_text = user_message.get("text", "")

            handler = getattr(cat, "langfuse_handler", None)

            # If handler is not yet initialized, create a new one
            if not handler:
                if settings.get("langfuse_public_key") and settings.get("langfuse_secret_key"):
                    handler = CallbackHandler(
                        public_key=settings["langfuse_public_key"],
                        secret_key=settings["langfuse_secret_key"],
                        host=settings.get("langfuse_host", "https://cloud.langfuse.com"),
                        user_id=cat.user_id,
                        session_id=cat.user_data.id,
                    )
                    cat.langfuse_handler = handler
                    log.info(f"Langfuse handler created for agent_fast_reply: {handler}")

            # Get or create a Langfuse Trace object
            # First, check if a trace was already set by the main LLM flow
            current_trace = getattr(handler, "trace", None)

            # If no trace from LLM, check if we manually created one for fast_reply
            if not current_trace:
                current_trace = getattr(cat, "langfuse_fast_reply_trace", None)

            # If still no trace, create a new one explicitly for this fast reply scenario
            if not current_trace:
                if handler:
                    try:
                        lf_client = Langfuse(
                            public_key=settings["langfuse_public_key"],
                            secret_key=settings["langfuse_secret_key"],
                            host=settings.get("langfuse_host", "https://cloud.langfuse.com"),
                        )
                        trace = lf_client.trace(
                            name="Agent Fast Reply Trace",
                            user_id=cat.user_id,
                            session_id=cat.user_data.id,
                            input=input_text,
                        )
                        # Keep reference to flush later if needed
                        cat.langfuse_client = lf_client
                        cat.langfuse_fast_reply_trace = trace # Store this trace on cat
                        current_trace = trace
                        log.info(f"Langfuse trace created for agent_fast_reply (explicitly): {trace.id}")
                    except Exception as trace_e:
                        log.error(f"Failed to create explicit Langfuse trace in agent_fast_reply: {trace_e}")
                        return fast_reply # If trace creation fails, return early
                else:
                    log.warning("Langfuse handler not initialized, cannot create trace for fast reply.")
                    return fast_reply


            if current_trace:
                # Now current_trace should always be a valid Trace object
                # Create a span for the fast reply
                span = current_trace.span(
                    name="agent_fast_reply",
                    input=input_text,
                    output=str(fast_reply["output"]),
                )
                # no debug print in production

                # Flush immediately as this is a fast reply and is the end of interaction
                if handler:
                    handler.flush()

    except Exception as e:
        log.error(f"Failed to trace agent_fast_reply: {e}")

    return fast_reply


@hook(priority=-1)
def fast_reply(fast_reply: Dict, cat: StrayCat) -> Dict:
    """Trace early fast replies that short-circuit the pipeline before the agent/LLM.

    If a plugin returns a dict with "output" here, the Cat replies immediately and
    skips the rest of the pipeline (including before_cat_sends_message).
    We therefore create/reuse a trace, add a span and flush+cleanup right away.
    """
    try:
        settings = cat.mad_hatter.get_plugin().load_settings()
        if not settings.get("enable_tracing", True):
            return fast_reply

        # Act only if a fast reply was actually produced
        if isinstance(fast_reply, dict) and "output" in fast_reply:
            # Safely read input text from WorkingMemory
            user_message_obj = getattr(cat.working_memory, "user_message_json", None)
            if isinstance(user_message_obj, dict):
                input_text = user_message_obj.get("text", "")
            else:
                input_text = getattr(user_message_obj, "text", "")

            handler = getattr(cat, "langfuse_handler", None)
            if not handler and settings.get("langfuse_public_key") and settings.get("langfuse_secret_key"):
                handler = CallbackHandler(
                    public_key=settings["langfuse_public_key"],
                    secret_key=settings["langfuse_secret_key"],
                    host=settings.get("langfuse_host", "https://cloud.langfuse.com"),
                    user_id=cat.user_id,
                    session_id=cat.user_data.id,
                )
                cat.langfuse_handler = handler

            current_trace = getattr(handler, "trace", None) if handler else None
            if not current_trace:
                current_trace = getattr(cat, "langfuse_fast_reply_trace", None)

            if not current_trace and handler:
                try:
                    lf_client = Langfuse(
                        public_key=settings["langfuse_public_key"],
                        secret_key=settings["langfuse_secret_key"],
                        host=settings.get("langfuse_host", "https://cloud.langfuse.com"),
                    )
                    trace = lf_client.trace(
                        name="Fast Reply Trace",
                        user_id=cat.user_id,
                        session_id=cat.user_data.id,
                        input=input_text,
                    )
                    cat.langfuse_client = lf_client
                    cat.langfuse_fast_reply_trace = trace
                    current_trace = trace
                except Exception as e:
                    log.error(f"Failed to create Langfuse trace in fast_reply: {e}")
                    return fast_reply

            if current_trace:
                try:
                    span = current_trace.span(
                        name="fast_reply",
                        input=input_text,
                        output=str(fast_reply["output"]),
                    )
                    # Update top-level trace fields so the response is visible on the trace
                    try:
                        current_trace.update(output=str(fast_reply["output"]), input=input_text)
                    except Exception as upd_e:
                        log.error(f"Failed to update Langfuse trace in fast_reply: {upd_e}")
                    if handler:
                        handler.flush()
                    if hasattr(cat, "langfuse_client") and cat.langfuse_client:
                        try:
                            cat.langfuse_client.flush()
                        except Exception as flush_e:
                            log.error(f"Failed to flush Langfuse client in fast_reply: {flush_e}")
                except Exception as e:
                    log.error(f"Failed to create/flush Langfuse span in fast_reply: {e}")

            # Cleanup here because finalizer won't run in this path
            try:
                if handler and hasattr(cat, "_llm") and hasattr(cat._llm, "callbacks") and cat._llm.callbacks is not None:
                    cat._llm.callbacks = [cb for cb in cat._llm.callbacks if cb is not handler]
                if hasattr(cat, "langfuse_handler"):
                    delattr(cat, "langfuse_handler")
                if hasattr(cat, "langfuse_handler_injected"):
                    delattr(cat, "langfuse_handler_injected")
                if hasattr(cat, "langfuse_fast_reply_trace"):
                    delattr(cat, "langfuse_fast_reply_trace")
                if hasattr(cat, "langfuse_client"):
                    delattr(cat, "langfuse_client")
            except Exception:
                pass

    except Exception as e:
        log.error(f"Failed to trace fast_reply: {e}")

    return fast_reply

@hook
def before_cat_sends_message(message: Dict, cat: StrayCat) -> Dict:
    """Finalize Langfuse traces at the end of LLM/agent interactions.

    - Updates the current trace with original user input and final AI output.
    - Adds `message.langfuse_trace_id` for client-side reference (LLM/agent paths only).
    - Performs cleanup to ensure the next interaction starts from a clean state.

    Note: this finalizer does not run on the early `fast_reply` path; in that case, the plugin
    flushes immediately within the `fast_reply` hook and no `trace_id` is added to the message.
    """
    handler = getattr(cat, "langfuse_handler", None)
    current_trace = getattr(handler, "trace", None)
    if not current_trace:
        current_trace = getattr(cat, "langfuse_fast_reply_trace", None)

    try:
        # The handler might not exist if initialization failed or if no LLM was called.
        llm_trace = current_trace

        if llm_trace:
            final_output = message.get("content", "")
            # The LLM prompt might be complex; we update the trace with the clean, original user text.
            user_input_text = cat.working_memory.get("user_message_json", {}).get(
                "text", ""
            )
            llm_trace.update(output=final_output, input=user_input_text)

            # Add the trace ID to the response message for client-side reference
            if hasattr(llm_trace, "id"):
                message.langfuse_trace_id = llm_trace.id
    except Exception as e:
        log.error(f"Error during Langfuse trace finalization: {e}")
    finally:
        # Cleanup for the next interaction to prevent state leaks.
        if (
            handler
            and hasattr(cat._llm, "callbacks")
            and cat._llm.callbacks is not None
        ):
            # Remove the specific handler instance from the list.
            cat._llm.callbacks = [cb for cb in cat._llm.callbacks if cb is not handler]
        if hasattr(cat, "langfuse_handler"):
            delattr(cat, "langfuse_handler")
        if hasattr(cat, "langfuse_handler_injected"):
            delattr(cat, "langfuse_handler_injected")
        if hasattr(cat, "langfuse_fast_reply_trace"):
            delattr(cat, "langfuse_fast_reply_trace")

    return message


@hook
def on_cat_shutdown(cat: StrayCat) -> None:
    """Flush any pending Langfuse traces when the Cat shuts down.

    This is a safeguard to ensure that any buffered traces are sent to
    Langfuse before the application terminates.
    """
    handler = getattr(cat, "langfuse_handler", None)
    if handler:
        try:
            handler.flush()
            log.info("Langfuse handler flushed on shutdown.")
        except Exception as e:
            log.error(f"Error flushing Langfuse handler on shutdown: {e}")
