# Langfuse Connector for Cheshire Cat

Integrates [Langfuse](https://langfuse.com/) with the Cheshire Cat AI to provide detailed, real-time tracing and observability for all Large Language Model (LLM) interactions.

This plugin allows you to monitor costs, debug issues, and gain deep insights into your AI's performance by capturing every LLM execution within your Langfuse project.

## Features

- **Automatic Tracing**: Automatically captures every LLM call made through the Cheshire Cat.
- **Rich Context**: Each trace includes the user's input, the final AI output, the `user_id`, and the `session_id` to group interactions by conversation.
- **Easy Configuration**: Set up the integration in seconds through the Cheshire Cat's admin panel.
- **Self-Hosting Support**: Works with both Langfuse Cloud and self-hosted instances.

## Installation and Configuration

1.  **Place the Plugin**: Add the `langfuse_connector` folder into your Cheshire Cat's `core/cat/plugins` directory.
2.  **Install Dependencies**: The plugin uses the `langfuse` library, which will be installed when activating it.
3.  **Configure in Admin Panel**:
    - Open the Cheshire Cat's admin panel in your browser.
    - Navigate to the "Plugins" section.
    - Find the "Langfuse Connector" plugin and click on its settings icon.
    - Enter your **Langfuse Public Key**, **Langfuse Secret Key**, and (if necessary) your self-hosted **Langfuse Host**.
    - Activate the `trace_enabled` option to start tracing.
    - Save the settings.

The plugin will now automatically start tracing all subsequent LLM interactions.

## How It Works

This plugin uses a simple and robust hook-based approach:

-   `agent_prompt_prefix`: Just before an LLM is called, this hook injects the `Langfuse CallbackHandler`. This handler is responsible for automatically creating a trace when the LLM is executed.
-   `before_cat_sends_message`: At the end of the interaction, this hook finds the trace created by the handler and updates it with the original user input and the final AI output, ensuring the trace is complete and accurate. It also handles cleanup to prepare for the next interaction.

This design ensures that tracing is activated only when needed and that all relevant information is captured correctly without interfering with other plugins.

