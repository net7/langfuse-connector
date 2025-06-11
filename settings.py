from pydantic import BaseModel, Field
from cat.mad_hatter.decorators import plugin


class LangfuseConnectorSettings(BaseModel):
    """Settings for the Langfuse Connector plugin."""

    enable_tracing: bool = Field(
        default=False,
        title="Enable Tracing",
        description="If checked, all LLM interactions will be traced on Langfuse.",
    )
    langfuse_public_key: str = Field(
        default="",
        title="Langfuse Public Key",
        description="Your Langfuse project's Public Key. You can get it from your project settings in Langfuse.",
    )
    langfuse_secret_key: str = Field(
        default="",
        title="Langfuse Secret Key",
        description="Your Langfuse project's Secret Key. This is a secret and should be handled with care.",
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        title="Langfuse Host",
        description="The host of the Langfuse server. Defaults to Langfuse Cloud.",
    )


@plugin
def settings_model():
    """Plugin settings for Langfuse integration."""
    return LangfuseConnectorSettings
