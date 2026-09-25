"""Helpers for deterministic prompt fingerprints."""

from langchain_core.prompts import ChatPromptTemplate


def prompt_messages(prompt: ChatPromptTemplate) -> list[dict[str, str]]:
    """Return the stable role/template fields that affect model output."""
    return [
        {
            "role": type(message).__name__,
            "template": message.prompt.template,
        }
        for message in prompt.messages
    ]

