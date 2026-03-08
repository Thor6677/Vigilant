from app.agent.providers.base import BaseLLMProvider
from app.config import get_settings


def get_provider() -> BaseLLMProvider:
    settings = get_settings()
    provider = settings.llm_provider.lower()

    if provider == "anthropic":
        from app.agent.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    elif provider == "ollama":
        from app.agent.providers.ollama_provider import OllamaProvider
        return OllamaProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}. Use 'anthropic' or 'ollama'.")
