"""Agent provider abstraction for be-conductor.

This package defines the contract every coding-agent backend must satisfy
to be used by a be-conductor structured-view session. The contract is
shaped around OpenCode's surface (the broadest, most generic agent SDK
we integrate); native SDKs like Claude advertise additional capabilities
on top via capability flags.

**Phase A status (May 2026):** the interface lives here as code, but
nothing in production yet uses it. The existing Claude `AgentSession`
([be_conductor/sessions/agent_session.py]) is unchanged and remains the
only path serving agent sessions. The `OpenCodeProvider` adapter is
implemented in Phase B.

See [docs/planned/agent-abstraction.md] for the design rationale,
the deep-study findings that produced this interface, and the
implementation phasing.
"""

from be_conductor.sessions.providers.base import (
    AgentEvent,
    AgentProvider,
    Capability,
    PROVIDER_NAME_CLAUDE,
    PROVIDER_NAME_OPENCODE,
)

__all__ = [
    "AgentEvent",
    "AgentProvider",
    "Capability",
    "PROVIDER_NAME_CLAUDE",
    "PROVIDER_NAME_OPENCODE",
]
