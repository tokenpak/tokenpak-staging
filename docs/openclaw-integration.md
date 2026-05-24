# OpenClaw Integration Guide

Connect TokenPak to OpenClaw for automatic context compression across all your agents.

---

## Prerequisites

- [ ] Python 3.10+
- [ ] TokenPak installed (`pip install tokenpak`)
- [ ] OpenClaw installed and configured
- [ ] At least one API key set (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)

---

## Quick Setup (3 steps)

### Step 1: Install & Configure

```bash
pip install tokenpak
tokenpak init --for openclaw
```

Expected output:
```
🚀 TokenPak Configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔑 API Key Discovery
  ✓ anthropic          auto-discovered (ANTHROPIC_API_KEY)

📝 Generating Config
  ✓ Config written     /home/you/.tokenpak/config.yaml
  ✓ Mode               openclaw
```

### Step 2: Run Inject Script

```bash
bash ~/.local/bin/tokenpak-inject.sh
```

This routes all OpenClaw providers through the TokenPak proxy.

### Step 3: Verify

```bash
tokenpak start
tokenpak verify
```

---

## How It Works

```
OpenClaw Agent
     │
     │ API request (large prompt)
     ▼
TokenPak Proxy (localhost:8766)
     │
     │ compressed request (30–60% smaller)
     ▼
LLM Provider (Anthropic / OpenAI / Google)
```

The `tokenpak-inject.sh` script:
1. Mirrors every provider as `tokenpak-<name>` pointing to the proxy
2. Updates model chains to route through proxy first
3. Runs automatically before `openclaw-gateway` starts (via systemd `ExecStartPre`)

---

## Systemd Service (Auto-Start)

Set up TokenPak to start automatically with OpenClaw:

```bash
# Create service
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/tokenpak-proxy.service << 'EOF'
[Unit]
Description=TokenPak LLM Proxy
After=network.target

[Service]
Type=simple
ExecStartPre=/home/you/.local/bin/tokenpak-inject.sh
ExecStart=/home/you/.local/bin/tokenpak start --no-daemon
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now tokenpak-proxy.service
```

---

## Troubleshooting

### Proxy not responding

```
✗ Proxy running   not responding
  → Fix: tokenpak start
```

**Fix:**
```bash
tokenpak start
# or via systemd:
systemctl --user start tokenpak-proxy.service
```

### API keys not found

```
✗ API keys   none found in environment
  → Fix: export ANTHROPIC_API_KEY='sk-ant-...'
```

**Fix — add to your shell profile (`~/.bashrc` or `~/.zshenv`):**
```bash
export ANTHROPIC_API_KEY='sk-ant-your-key-here'
```

Then reload: `source ~/.bashrc`

### Port 8766 already in use

```bash
# Find what's using it
lsof -i :8766

# Change TokenPak port
export TOKENPAK_PORT=8767
tokenpak start
```

Update `~/.tokenpak/config.yaml`:
```yaml
proxy:
  port: 8767
```

### Config invalid / YAML error

```bash
# Regenerate config
tokenpak init --force
```

### Double-proxy detection

If you see `localhost:8766` in a non-tokenpak provider entry, the inject script will warn:
```
⚠ Non-tokenpak provider 'anthropic' points to localhost:8766 — possible double-proxy
```

**Fix:** Remove the `baseUrl` from the original provider entry in `~/.openclaw/openclaw.json`.

---

## FAQ

**Q: Does TokenPak modify my original API keys?**  
A: No. It creates `tokenpak-*` mirror providers that route through the proxy. Your original providers are unchanged.

**Q: Will compression change model responses?**  
A: Compression removes redundant whitespace and repetition while preserving meaning. For most use cases, responses are identical.

**Q: Can I use TokenPak with multiple agents?**  
A: Yes. Run one proxy (`tokenpak start`) and all OpenClaw agents use it automatically via the inject script.

**Q: How do I check how much I'm saving?**  
A: `tokenpak cost` or `tokenpak savings`

---

**More help:** `tokenpak doctor --fix` · `tokenpak verify` · https://tokenpak.dev/docs
