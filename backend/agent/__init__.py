"""Lazy legacy exports; importing business schemas never loads the agent."""
__all__ = ['AgentSession','BankingAgent','OpenAICompatibleLLMClient','RetryingLLMClient']
def __getattr__(name):
    if name == 'AgentSession':
        from .session import AgentSession
        return AgentSession
    if name == 'BankingAgent':
        from .agent import BankingAgent
        return BankingAgent
    if name in {'OpenAICompatibleLLMClient','RetryingLLMClient'}:
        from . import llm_client
        return getattr(llm_client,name)
    raise AttributeError(name)
