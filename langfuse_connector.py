"""Langfuse Connector Plugin for Cheshire Cat.

This plugin integrates Langfuse with the Cheshire Cat to provide detailed, real-time
tracing and observability for all Large Language Model (LLM) interactions.

The tracing logic is designed to be robust and simple:
- It uses the `agent_prompt_prefix` hook to reliably inject the Langfuse
  `CallbackHandler` just before an LLM is called.
- The `CallbackHandler` automatically creates a trace upon LLM execution.
- The `before_cat_sends_message` hook finalizes the trace by adding the
  original user input and the final AI output, ensuring the trace is
  complete and accurate.
- A final cleanup step runs after each message to ensure interactions are
  isolated and no state leaks.
"""

from typing import Dict
from langfuse.callback import CallbackHandler
from cat.mad_hatter.decorators import hook, endpoint
from cat.log import log
from cat.looking_glass.stray_cat import StrayCat

from fastapi import Body, Depends
import requests
from requests.auth import HTTPBasicAuth
from cat.auth.connection import HTTPAuth
from cat.auth.permissions import AuthResource, AuthPermission


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


@hook
def before_cat_sends_message(message: Dict, cat: StrayCat) -> Dict:
    """Finalize the Langfuse trace at the end of an interaction.

    This hook finds the trace created by the CallbackHandler and updates it with
    the complete context: the original user input and the final AI output.
    This ensures the trace in Langfuse is accurate and easy to understand.

    It also performs a critical cleanup step, removing the handler and flags
    to ensure the next user interaction starts from a clean state.
    """
    handler = getattr(cat, "langfuse_handler", None)

    try:
        # The handler might not exist if initialization failed or if no LLM was called.
        llm_trace = getattr(handler, "trace", None)

        if llm_trace:
            final_output = message.get("content", "")
            # The LLM prompt might be complex; we update the trace with the clean, original user text.
            user_input_text = cat.working_memory.get("user_message_json", {}).get(
                "text", ""
            )
            llm_trace.update(output=final_output, input=user_input_text)

            # Add the trace ID to the response message for client-side reference
            if hasattr(llm_trace, "id"):
                message["trace_id"] = llm_trace.id
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


@endpoint.post(path="/feedback", prefix="")
async def feedback(
        body: dict = Body(...),
        cat: StrayCat = Depends(HTTPAuth(AuthResource.MEMORY, AuthPermission.READ)),
):
    """
    Receive and process user feedback, saving it to both the Cat's
    internal memory (Qdrant) and an external observability platform (Langfuse).
    """
    message_id = body.get("message_id")
    trace_id = body.get("trace_id")
    user_id = body.get("user_id")
    
    # Get structured feedback from the request body
    punteggio = int(body.get("punteggio", -1))
    feedback_problem = body.get("feedback_problem", "")
    feedback_description = body.get("feedback_description", "")

    # Basic validation: punteggio must be 0 (dislike) or 1 (like)
    if punteggio not in [0, 1] or not message_id or not trace_id or not user_id:
        return False

    try:
        vector_memory = cat.memory.vectors
        points, _ = vector_memory.vector_db.scroll(
            collection_name="episodic",
            scroll_filter={
                "must": [
                    {
                        "key": "metadata.message_id",
                        "match": {
                            "value": message_id
                        }
                    }
                ]
            },
            with_payload=True,
            limit=1
        )
        if not points:
            log.warning(f"Could not find message with ID {message_id} to attach feedback.")
            return False
        point = points[0]

        existing_full_payload = point.payload
        if not existing_full_payload:
            log.warning(f"Message with ID {message_id} found but has no payload.")
            return False

        inner_metadata = existing_full_payload.get("metadata", {})
        updated_inner_metadata = inner_metadata.copy()

        db_user_id = updated_inner_metadata.get("user_id")
        request_user_id = user_id
        log.error(f"Feedback check: User ID from DB is '{db_user_id}', User ID from request is '{request_user_id}'.")

        if updated_inner_metadata.get("user_id") != user_id:
            log.warning("User attempting to provide feedback does not match message owner.")
            return False
        
        # Update Qdrant metadata with the new structured feedback
        outcome_str = "positivo" if punteggio == 1 else "negativo"
        updated_inner_metadata["feedback_outcome"] = outcome_str
        updated_inner_metadata["feedback_problem"] = feedback_problem
        updated_inner_metadata["feedback_description"] = feedback_description
        if "feedback" in updated_inner_metadata: # Clean up old generic field if present
            del updated_inner_metadata["feedback"]
        if "punteggio" in updated_inner_metadata: # Clean up old key
            del updated_inner_metadata["punteggio"]

        new_payload_for_qdrant = {
            "page_content": existing_full_payload.get("page_content", " "),
            "metadata": updated_inner_metadata
        }

        # Overwrite the payload in Qdrant
        vector_memory.vector_db.set_payload(
            collection_name="episodic",
            payload=new_payload_for_qdrant,
            points=[point.id]
        )
    except Exception as e:
        log.error(f"An error occurred during Qdrant operation for feedback: {e}")
        # Return False on any database-related error to prevent a 500 crash.
        return False

    settings = cat.mad_hatter.get_plugin().load_settings()

    if not settings.get("enable_tracing", True):
        return True

    # Prepare dynamic payload for Langfuse
    langfuse_payload = {
        "traceId": trace_id,
        "dataType": "BOOLEAN",
    }
    
    if punteggio == 1: # Positive feedback
        langfuse_payload["name"] = "Valutazione positiva"
        langfuse_payload["value"] = 1
        langfuse_payload["comment"] = "" # No detailed comment for positive feedback
    else: # Negative feedback
        langfuse_payload["name"] = feedback_problem if feedback_problem else "Valutazione negativa"
        langfuse_payload["value"] = 0
        langfuse_payload["comment"] = feedback_description

    base_url = settings.get("langfuse_host", "https://cloud.langfuse.com")
    url = f"{base_url}/api/public/scores"
    headers = {"Content-Type": "application/json"}

    if settings.get("langfuse_public_key") and settings.get("langfuse_secret_key"):
        try:
            response = requests.post(
                url,
                auth=HTTPBasicAuth(settings["langfuse_public_key"], settings["langfuse_secret_key"]),
                headers=headers,
                json=langfuse_payload
            )
            response.raise_for_status()
            log.info(f"Langfuse feedback response: {response.json()}")
        except requests.exceptions.RequestException as e:
            log.error(f"Error sending feedback to Langfuse: {e}")
        except Exception as e:
            log.error(f"An unexpected error occurred when processing the Langfuse response: {e}")
    else:
        log.warning("Langfuse API keys not configured. Skipping feedback score submission.")
        
    return True
