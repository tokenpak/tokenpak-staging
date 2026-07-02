# tokenpak-agents

Unified multi-agent framework integration package for TokenPak.

## Included Integrations
- `tokenpak_agents.autogen`: AutoGen message, assistant, and group chat wrappers
- `tokenpak_agents.semantic_kernel`: Semantic Kernel-oriented memory utilities

## Install

```bash
pip install -e .
```

Optional extras:

```bash
pip install -e .[autogen]
pip install -e .[semantic-kernel]
pip install -e .[all]
```

## Quick Usage

```python
from tokenpak_agents.autogen import TokenPakAssistant
from tokenpak_agents.semantic_kernel import TokenPakMemory

assistant = TokenPakAssistant(name="agent", budget=4000)
memory = TokenPakMemory(budget=4000)
```
