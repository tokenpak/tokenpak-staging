# TokenPak Python SDK: Installation Guide

## System Requirements

- **Python version:** 3.10 or later
- **OS:** Linux, macOS, or Windows
- **pip:** Version 20.0 or later (usually included with Python)

## Basic Installation

The simplest way to install TokenPak:

```bash
pip install tokenpak
```

For local installs, use a virtual environment before running the same command:

```bash
python -m venv .venv
source .venv/bin/activate
pip install tokenpak
```

If `pip` reports an externally managed Python environment (PEP 668), use a
virtual environment or `pipx install tokenpak` instead of forcing writes into
the system Python.

This installs the core library with heuristic-based compression.

## Optional Dependencies

TokenPak has optional extras for advanced features:

```bash
# Accurate token counting
pip install tokenpak[tokens]

# LLM-based compression engine
pip install tokenpak[compression]

# Semantic/vector retrieval
pip install tokenpak[retrieval]

# Restore the legacy bundled install behavior
pip install tokenpak[full]
```

## Using a Virtual Environment (Recommended)

**Option 1: venv (built-in)**

```bash
# Create a virtual environment
python3 -m venv ~/my_tokenpak_env

# Activate it
source ~/my_tokenpak_env/bin/activate # Linux/macOS
# or
~/my_tokenpak_env\Scripts\activate # Windows

# Install TokenPak
pip install tokenpak

# Deactivate when done
deactivate
```

**Option 2: uv (faster, modern)**

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create and activate environment
uv venv ~/my_tokenpak_env
source ~/my_tokenpak_env/bin/activate

# Install TokenPak
pip install tokenpak
```

## Verify Your Installation

After installing, verify that TokenPak works:

```bash
tokenpak --version
```

Or run a quick test:

```bash
python3 -c "import tokenpak; print(tokenpak.__version__)"
```

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'tokenpak'"

**Solution:** Make sure you're running Python from the correct environment:
```bash
which python3 # Should show path in your venv
pip list | grep tokenpak # Should show tokenpak installed
```

If not in a venv, reinstall:
```bash
pip install --upgrade tokenpak
```

### Issue: "tiktoken not found" or encoding errors

**Solution:** Install the token-counting extra:
```bash
pip install tokenpak[tokens]
```

### Issue: "Permission denied" when installing

**Solution:** Use a virtual environment. `--user` can work on unmanaged Python
installs, but PEP 668-managed system Python installs should use a venv or
`pipx install tokenpak`.

```bash
python3 -m venv ~/.venvs/tokenpak
source ~/.venvs/tokenpak/bin/activate
pip install tokenpak
```

### Issue: Python version error (3.9 or earlier)

**Solution:** TokenPak requires Python 3.10+. Upgrade Python or use a package manager:
```bash
# macOS (homebrew)
brew install python@3.11

# Ubuntu/Debian
sudo apt-get install python3.11

# Then install TokenPak with the new version
python3.11 -m pip install tokenpak
```

## Upgrading TokenPak

To upgrade to the latest version:

```bash
pip install --upgrade tokenpak
```

## What's Next?

- **[Quick Start Guide](./quickstart.md)** — Get running in 5 minutes
- **[API Reference](./api-reference.md)** — Explore the full API
- **[Examples](../examples/)** — Real-world usage patterns
