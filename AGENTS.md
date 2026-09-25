# Oryn project guide

Oryn is an educational, Hermes-inspired local coding assistant. The main goal is to understand how an agent harness works, so keep changes small and explain important design decisions.

- The user owns the architecture. Explain a proposed new subsystem and get approval before implementing it.
- Keep the Codex provider, conversation loop, context handling, session storage, and tools as separate responsibilities.
- Prefer Python's standard library and the simplest design that fits the current project.
- Treat placeholder modules as unimplemented; describe only behavior that exists in code.
