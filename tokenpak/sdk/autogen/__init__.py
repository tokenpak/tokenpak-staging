"""TokenPak integration for Microsoft AutoGen.

This package provides automatic context compression for AutoGen conversations,
reducing token usage in multi-agent systems while preserving conversation quality.

Example:
    >>> from tokenpak.sdk.autogen import TokenPakConversationHook
    >>> from autogen import UserProxyAgent, AssistantAgent
    >>>
    >>> hook = TokenPakConversationHook()
    >>>
    >>> user = UserProxyAgent("user")
    >>> assistant = AssistantAgent("assistant", llm_config={...})
    >>>
    >>> hook.compress_agent(assistant)
    >>>
    >>> user.initiate_chat(assistant, message="Hello!")
"""

from .context import (
    AgentContextConfig,
    TokenPakAssistant,
    TokenPakCompressionReport,
    TokenPakConversationHook,
)
from .message import compress_messages

__version__ = "0.1.0"
__all__ = ['TokenPakConversationHook', 'TokenPakAssistant', 'TokenPakCompressionReport', 'AgentContextConfig', 'compress_messages', 'assistant', 'context', 'examples', 'groupchat', 'message', 'tests']
