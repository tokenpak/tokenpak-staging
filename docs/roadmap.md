# TokenPak Roadmap

This document outlines the currently visible TokenPak planning surface. It is not a release promise; items move only after implementation, tests, and release-gate evidence exist.

## Current shipped surface

**Shipped:**
- Deterministic compression engine
- Multi-mode compression (hybrid, aggressive, minimal)
- Vault context injection (BM25)
- CANON deduplication
- Prompt caching integration
- CLI tools (`tokenpak compress`, `tokenpak cost`, `tokenpak doctor`)
- Local telemetry and monitoring
- HTTP proxy server
- systemd service support
- Apache-2.0 licensed

---

## Current priorities

- [ ] Performance optimization for compression hot paths
- [ ] Plugin system for custom compressors
- [ ] WebSocket support
- [ ] OpenTelemetry export
- [ ] Docker official image
- [ ] Cloud dashboard
- [ ] Usage analytics
- [ ] Cost tracking across projects

---

## Larger candidates

- [ ] Stable API (no breaking changes)
- [ ] Multi-provider support (Anthropic, OpenAI, Google, Mistral)
- [ ] Streaming compression
- [ ] Batch processing mode
- [ ] Comprehensive test suite
- [ ] Managed proxy service
- [ ] API key management

---

## Feature Requests

Have an idea? Open an issue on GitHub with the `feature-request` label.

### Under Consideration
- GraphQL support
- gRPC compression
- Browser extension
- VS Code extension
- Prometheus metrics exporter
- Kubernetes operator

### Not Planned
- Model hosting (out of scope)
- Fine-tuning (out of scope)
- Prompt engineering (separate tool)

---

## Release Cadence

- Monthly releases (patch), quarterly features (minor)

## Versioning

We follow [Semantic Versioning](https://semver.org/):
- MAJOR: Breaking API changes
- MINOR: New features, backward compatible
- PATCH: Bug fixes, backward compatible

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for how to get involved in development.
