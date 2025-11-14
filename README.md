# Langfuse Connector for Cheshire Cat

Integrates [Langfuse](https://langfuse.com/) with the Cheshire Cat AI to provide detailed, real-time tracing and observability for all interactions, including LLM calls and fast/agent fast replies.

This plugin allows you to monitor costs, debug issues, and gain deep insights into your AI's performance by capturing every LLM execution within your Langfuse project.

## Features

- **Automatic Tracing (LLM)**: Automatically captures every LLM call made through the Cheshire Cat.
- **Fast Reply Tracing**: Traces early short-circuit replies produced by the `fast_reply` hook (before agent/LLM), with immediate flush to Langfuse.
- **Agent Fast Reply Tracing**: Traces replies produced by the `agent_fast_reply` hook (after recall, before LLM).
- **Rich Context**: Each trace includes the user's input, the final AI output, the `user_id`, and the `session_id` to group interactions by conversation (when available for that path).
- **Easy Configuration**: Set up the integration in seconds through the Cheshire Cat's admin panel.
- **Self-Hosting Support**: Works with both Langfuse Cloud and self-hosted instances.

## Installation and Configuration

1.  **Place the Plugin**: Add the `langfuse_connector` folder into your Cheshire Cat's `core/cat/plugins` directory.
2.  **Install Dependencies**: The plugin depends on the `langfuse` Python SDK.
    - Requirements: `langfuse>=3,<4` (compatible with Langfuse server/web 3.x)
3.  **Configure in Admin Panel**:
    - Open the Cheshire Cat's admin panel in your browser.
    - Navigate to the "Plugins" section.
    - Find the "Langfuse Connector" plugin and click on its settings icon.
    - Enter your **Langfuse Public Key**, **Langfuse Secret Key**, and (if necessary) your self-hosted **Langfuse Host**.
    - Enable the `Enable Tracing` option to start tracing.
    - Save the settings.

The plugin will now automatically start tracing:
- LLM interactions; and
- Early short-circuit replies produced by `fast_reply`; and
- Agent-level short-circuit replies produced by `agent_fast_reply`.

## How It Works

This plugin uses a simple and robust hook-based approach:

-   `agent_prompt_prefix`: Just before an LLM is called, this hook injects the `Langfuse CallbackHandler`. The handler will automatically create a trace when the LLM is executed.
-   `agent_fast_reply`: When a plugin returns a direct reply after memory recall (but before LLM), this hook creates a trace (if needed), adds a span and lets the finalizer complete the trace.
-   `fast_reply`: When a plugin returns an early reply that short-circuits the pipeline (before recall/agent/LLM), this hook creates a trace and a span and flushes immediately (since the finalizer won’t run on this path).
-   `before_cat_sends_message`: At the end of the interaction (LLM and agent paths), this hook updates the trace with the original user input and final AI output, and performs cleanup.

This design ensures that tracing is activated only when needed and that all relevant information is captured correctly without interfering with other plugins.

## Compatibility

- Python SDK: `langfuse` 3.x
- Server/Web: Langfuse 3.x (Cloud or self-hosted)

## Notes

- In the `fast_reply` path the response is sent immediately and the finalizer does not run; the plugin flushes traces and spans immediately, but does not add a `trace_id` to the outgoing message.
- In LLM/agent paths the finalizer adds `langfuse_trace_id` to the outgoing message for client-side reference.

