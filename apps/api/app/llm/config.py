"""Default LiteLLM model mapping by provider and tier."""

from app.llm.types import ModelTier, Provider

MODEL_TIERS: dict[Provider, dict[ModelTier, str]] = {
    Provider.ANTHROPIC: {
        ModelTier.FAST: "anthropic/claude-haiku-5-5",
        ModelTier.BALANCED: "anthropic/claude-sonnet-5-5",
        ModelTier.STRONG: "anthropic/claude-opus-5-5",
    },
    Provider.OPENAI: {
        ModelTier.FAST: "openai/gpt-6-luna",
        ModelTier.BALANCED: "openai/gpt-6-sol",
        ModelTier.STRONG: "openai/gpt-6.1-sol",
    },
    Provider.GEMINI: {
        ModelTier.FAST: "gemini/gemini-3.8-flash",
        ModelTier.BALANCED: "gemini/gemini-4-argon",
        ModelTier.STRONG: "gemini/gemini-4-argon",
    },
}
