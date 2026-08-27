"""Optional LLM adapter. Installed with `pip install 'dataassay[llm]'`.

Nothing in the core package imports this, and nothing here is reached unless
`assay interview` is run. The core audit works offline, forever.
"""

from dataassay.llm import interview, payload, provider

__all__ = ["interview", "payload", "provider"]
