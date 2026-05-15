"""Provider-neutral agent contract.

`AgentProvider` is the interface every coding-agent backend implements to
be driven by a be-conductor structured-view session. The base contract is
shaped around what OpenCode's Python SDK can do; native SDKs (Claude, and
possibly Codex / Gemini direct in the future) advertise additional
behaviors through `Capability` flags rather than through new interface
methods.

This module is **interface-only**. Importing it has no side effects, no
runtime dependencies on `claude-agent-sdk` or `opencode-ai`, and no
production code path uses it yet. Concrete provider implementations
live in sibling modules (e.g. `opencode.py`, `claude.py`) and are
loaded only by the orchestrator that needs them.

Design notes — see [docs/planned/agent-abstraction.md] for the full
rationale, in particular:

  - "Decided strategy": OpenCode defines the base, native SDKs extend.
  - Track 2 fitness check: the mapping of every Claude / OpenCode
    feature onto the base interface or onto a capability flag.
  - "Implementation phasing": Phase A (this file) — interface lives in
    code with no wiring; Phase B — the OpenCode adapter; Phase F (only
    if everything works) — Claude migrated onto this interface.

Conventions:

  - Methods that may need to do I/O are async, even when the underlying
    SDK call could be synchronous, so that the orchestrator can compose
    providers uniformly with asyncio.
  - `events()` is the single source of truth for what the agent is
    doing right now. The orchestrator broadcasts events to WebSocket
    subscribers and persists them to history.
  - The wire-protocol event names in `AgentEvent.type` are deliberately
    chosen to align with the events the existing frontend
    ([be_conductor/static/agent-view.html]) already handles, so the UI
    refactor in Phase C is gating logic only — no new event types in
    the frontend.
  - `AgentProvider.capabilities` is set at construction time and is
    immutable for a session's lifetime. The orchestrator emits it on
    `system_init` and the frontend uses it to gate widget visibility.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Literal, Protocol, TypedDict, runtime_checkable


# ---------------------------------------------------------------------------
# Provider name constants
#
# Used as `AgentProvider.name` and as the `provider` field on `system_init`
# events. Keep these stable: the frontend may special-case them for badge
# rendering.
# ---------------------------------------------------------------------------

PROVIDER_NAME_CLAUDE: str = "claude"
PROVIDER_NAME_OPENCODE: str = "opencode"

# ACP-backed agents share one provider class (AcpProvider) and report
# `name` as "acp-<agent>". The prefix is the stable dispatch key the
# registry matches on; the suffix selects the adapter binary.
PROVIDER_PREFIX_ACP: str = "acp-"


# ---------------------------------------------------------------------------
# Capability flags
#
# Capabilities are advertised by a provider at construction time. They tell
# the orchestrator (and the frontend, via `system_init`) which optional
# behaviors are available for this session.
#
# A capability flag means: "this provider can produce events / accept calls
# of this kind." Absence of a flag means the corresponding UI surface
# (widget, modal, button) should be hidden — *not* that an empty/disabled
# version should be shown. Designing for absence avoids the
# lowest-common-denominator look that hurts the Claude experience.
#
# When adding a new capability:
#   1. Add the constant here with a short docstring.
#   2. Add it to the matrix table in
#      [docs/planned/agent-abstraction.md] (Track 2).
#   3. Update the relevant provider's `capabilities` set if it should be
#      advertised.
#   4. Update the frontend gating in `agent-view.html` if a widget
#      depends on it.
#
# Capabilities are intentionally fine-grained. It is fine for a provider
# to advertise `tools` but not `tool_progress`, or `mid_turn_approval`
# but not `pre_tool_approval`. Coarser groupings would force providers
# into binary choices that don't match how the SDKs actually behave.
# ---------------------------------------------------------------------------


class Capability:
    """Capability flag namespace.

    Flags are plain strings rather than an Enum for two reasons:

    1. The set is forward-compatible: providers may advertise
       capabilities the running version of be-conductor doesn't know
       about, and unknown flags should round-trip through the wire
       protocol unchanged so a future client release can light them up.
    2. Wire-protocol stability: capability flags are part of the
       `system_init` event payload. Strings are stable; Python Enum
       member ordering and stringification is not always.

    Use `Capability.X` constants in code; they exist purely as a
    discoverability / typo-prevention aid.
    """

    # ----- content kinds the provider can produce ---------------------

    TEXT: str = "text"
    """Provider produces text-only assistant output. Always present."""

    STREAMING_DELTAS: str = "streaming_deltas"
    """Provider emits token-level deltas for live rendering.

    Maps to be-conductor's `stream_start` / `stream_delta` / `stream_stop`
    wire events. Without this flag, assistant messages still arrive
    (but only as completed snapshots).
    """

    REASONING: str = "reasoning"
    """Provider produces a separate `reasoning` / `thinking` block kind.

    Anthropic calls these "thinking" blocks; OpenCode calls them
    `reasoning` parts. Both are dimmed/italic in the UI. Frontend
    renders these only when the flag is set.
    """

    # ----- tool use ---------------------------------------------------

    TOOLS: str = "tools"
    """Provider exposes tool calls as discrete events.

    Without this, tool use (if it happens at all) is invisible to the
    UI and effectively folded into assistant text.
    """

    TOOL_PROGRESS: str = "tool_progress"
    """Provider streams intermediate tool state (partial output, status
    transitions) before the tool completes."""

    # ----- turn structure ---------------------------------------------

    MULTI_STEP_TURN: str = "multi_step_turn"
    """A single user prompt may produce multiple bracketed steps
    (tool-use step, then text step, etc.). Providers without this
    advertise a single step per turn."""

    # ----- accounting -------------------------------------------------

    COST_REPORTING: str = "cost_reporting"
    """Provider reports per-turn cost in USD."""

    TOKEN_USAGE: str = "token_usage"
    """Provider reports per-turn token counts."""

    CONTEXT_USAGE: str = "context_usage"
    """Provider can report current context window usage on demand
    (drives the be-conductor context ring)."""

    # ----- runtime control --------------------------------------------

    MODEL_SWITCHING: str = "model_switching"
    """The model can be changed at runtime (per-turn or per-session)."""

    AGENT_SWITCHING: str = "agent_switching"
    """The agent / mode (build / plan / general / custom) can be
    changed at runtime."""

    CANCEL: str = "cancel"
    """Running turns can be cancelled via `interrupt()`."""

    SESSION_RESUME: str = "session_resume"
    """Existing sessions can be reattached by ID (survives orchestrator
    restart and, where supported, server restart)."""

    # ----- approval and interaction -----------------------------------

    PERMISSION_MODES: str = "permission_modes"
    """Provider has per-session permission modes
    (e.g. Claude's default / acceptEdits / bypassPermissions / plan)."""

    PRE_TOOL_APPROVAL: str = "pre_tool_approval"
    """Provider supports synchronous approve-before-tool-runs
    (Claude's `can_use_tool`). Without this, approval is event-driven
    only."""

    MID_TURN_APPROVAL: str = "mid_turn_approval"
    """Provider can pause mid-turn to ask the user a question
    (Claude's `AskUserQuestion`, OpenCode's `permission.asked`)."""

    PLAN_REVIEW: str = "plan_review"
    """Provider supports the structured plan-then-approve flow
    (Claude's `ExitPlanMode`)."""

    # ----- Claude-specific niceties (kept honest) ---------------------

    EFFORT_LEVELS: str = "effort_levels"
    """Provider supports an effort dial (low / medium / high / max)."""

    ADAPTIVE_THINKING: str = "adaptive_thinking"
    """Provider supports the adaptive-thinking control
    (auto / summary / off)."""

    COMPACT_BOUNDARY: str = "compact_boundary"
    """Provider emits explicit context-compaction events."""

    RATE_LIMIT_EVENTS: str = "rate_limit_events"
    """Provider emits structured rate-limit telemetry events
    distinct from generic errors."""

    # ----- ecosystem --------------------------------------------------

    SUBAGENTS: str = "subagents"
    """Provider can spawn sub-agents with their own context."""

    MCP: str = "mcp"
    """Provider integrates with MCP servers."""

    SKILLS: str = "skills"
    """Provider has a skill / slash-command system."""

    BTW_SIDECHANNEL: str = "btw_sidechannel"
    """Provider supports be-conductor's "by the way" out-of-band query
    (a transient question that doesn't disturb the main turn queue)."""


# ---------------------------------------------------------------------------
# AgentEvent — the wire-compatible event shape
#
# This is the *output* the orchestrator broadcasts. Each provider's
# `events()` iterator yields these. The shape is intentionally tolerant
# (TypedDict total=False) because event payloads vary by type and we
# don't want to force a giant tagged-union per concrete event class on
# adapter authors.
# ---------------------------------------------------------------------------


AgentEventType = Literal[
    # ----- session lifecycle -----
    "system_init",        # session opened, advertises provider + capabilities
    "session_end",        # session closed (clean exit or error)
    "turn_start",         # user prompt accepted, agent begins work
    "turn_end",           # agent finished one turn (= one user prompt cycle)
    # ----- content -----
    "user_message",       # echoes a user prompt (orchestrator may emit
                          # this itself; providers may also emit it for
                          # multi-client consistency)
    "assistant_message",  # complete assistant turn snapshot
    "stream_start",       # streaming block began (text or reasoning)
    "stream_delta",       # token chunk for an active block
    "stream_stop",        # streaming block ended
    # ----- tools -----
    "tool_use_start",     # tool invocation began
    "tool_use_progress",  # intermediate tool state (output streaming, etc.)
    "tool_use_end",       # tool finished (completed or error)
    # ----- interaction -----
    "permission_request", # provider needs user approval for something
    "permission_resolved",# approval decision was applied
    # ----- diagnostics -----
    "error",              # something went wrong (recoverable or not)
    "rate_limit",         # vendor rate-limit telemetry
    "system",             # catch-all for provider-specific signals
                          # (compact_boundary, settings change, etc.)
]


class AgentEvent(TypedDict, total=False):
    """The wire-compatible event a provider yields to the orchestrator.

    Required: `type`. Everything else is type-specific. Adapters should
    populate the conventional fields (documented next to each
    `AgentEventType` value in this module) so the orchestrator can
    forward without remapping. Unknown fields are forwarded as-is.

    Wire compatibility: keys here line up with what
    [be_conductor/static/agent-view.html] already consumes, so Phase C
    (frontend capability gating) is a *removal* of Claude-only code
    paths, not a rewrite of the renderer.
    """

    type: AgentEventType

    # Common envelope fields
    session_id: str
    turn_id: str
    provider: str           # one of PROVIDER_NAME_*
    capabilities: list[str] # only on system_init
    timestamp: float        # unix seconds; orchestrator may add if missing

    # Content fields (populated for the relevant event types)
    content: Any            # for assistant_message: list of parts
    text: str               # for stream_delta when delta_type == "text"
    thinking: str           # for stream_delta when delta_type == "thinking"
    delta_type: str         # "text" | "thinking" / "reasoning"
    block_type: str         # "text" | "thinking" / "reasoning"
    index: int              # block index within an assistant message

    # Tool fields
    tool: str               # tool name
    tool_use_id: str        # provider's stable id for the tool call
    input: dict[str, Any]   # tool input arguments
    output: Any             # tool output payload
    is_error: bool          # tool finished with an error

    # Permission flow
    request_id: str         # opaque id the orchestrator passes back
                            # to respond_to_permission()
    decision: str           # provider-specific (e.g. "once" / "always" /
                            # "reject"); orchestrator forwards verbatim

    # Result / accounting
    stop_reason: str
    duration_ms: int
    num_turns: int
    total_cost_usd: float
    usage: dict[str, Any]   # provider-specific token-usage breakdown
    model_usage: dict[str, Any]

    # Errors
    error: str

    # Catch-all
    subtype: str            # narrows generic `system` events
    payload: dict[str, Any] # adapter-defined extra context


# ---------------------------------------------------------------------------
# AgentProvider — the contract
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentProvider(Protocol):
    """Contract every coding-agent backend implements.

    The orchestrator (Phase B's `ProviderAgentSession`, future) holds
    one `AgentProvider` per session and treats it as the SDK-shaped
    seam between be-conductor's session machinery (queue, history,
    broadcast, multi-client) and the underlying agent.

    Lifecycle:
        1. Construct (provider-specific args, plus options)
        2. `await start()` — connect / authenticate / open subscriber
        3. `await send(...)` — queue a turn; orchestrator may call
           multiple times before any `turn_end` arrives
        4. Concurrently consume `events()` — async iterator yields
           `AgentEvent`s until the session ends
        5. `await stop()` — clean shutdown
        6. `await interrupt()` may be called any time during 3–4

    Concurrency notes:

      - `send()` and `interrupt()` are expected to be safe to call
        while `events()` is iterating in another task.
      - `events()` may yield indefinitely; the orchestrator drives
        cancellation via `stop()` or by cancelling the consuming task.
      - Providers should *not* assume single-consumer semantics for
        `events()` — but the orchestrator currently only iterates
        once, so a single-stream implementation is acceptable for v1.

    Capability negotiation:

      - `capabilities` is read after construction and reflected in the
        `system_init` event the orchestrator emits.
      - Providers must not advertise a capability they cannot actually
        deliver in this session (e.g. don't advertise `MCP` when no
        MCP servers are configured).
      - Optional methods on this protocol should `raise
        NotImplementedError` if the corresponding capability is not
        advertised; the orchestrator gates calls on the flag.
    """

    # ----- static metadata, set at construction -----------------------

    name: str
    """Stable provider identifier — see PROVIDER_NAME_* constants."""

    capabilities: set[str]
    """Capability flags this session advertises. Read after
    construction; immutable for the session's lifetime."""

    # ----- lifecycle --------------------------------------------------

    async def start(self) -> None:
        """Open the connection to the underlying agent.

        For OpenCode: authenticate the SDK client, create or attach to
        a session, subscribe to the global event stream.

        For Claude (future migration): instantiate ClaudeSDKClient,
        register hooks, await the first ready signal.

        Raises on unrecoverable setup failure. Recoverable issues
        (transient network, rate limits) should be retried internally
        and / or surfaced via `events()` as `error` / `rate_limit`.
        """
        ...

    async def stop(self) -> None:
        """Cleanly tear down. Called exactly once per session.

        Should drain pending events, close any subscriber/stream, and
        release SDK resources. After this returns, `events()` must
        complete (StopAsyncIteration).
        """
        ...

    async def interrupt(self) -> None:
        """Cancel any running turn ASAP without ending the session.

        Maps to OpenCode's `session.abort` and Claude SDK's interrupt.
        After interrupt, the session remains usable for new `send()`
        calls. Emits a `turn_end` event with an appropriate
        `stop_reason` (typically "cancelled" or "interrupted").
        """
        ...

    # ----- input ------------------------------------------------------

    async def send(
        self,
        *,
        text: str,
        attachments: list[dict] | None = None,
        model: str | None = None,
        agent: str | None = None,
        options: dict | None = None,
    ) -> None:
        """Submit a user turn.

        `text` is the prompt body. `attachments` are
        provider-recognized references (file paths, image data, etc.;
        the orchestrator passes through whatever it received). `model`
        and `agent` override the session defaults for *this* turn only
        and are ignored unless `MODEL_SWITCHING` / `AGENT_SWITCHING`
        is advertised. `options` is a per-provider escape hatch — keep
        empty for v1.

        The call returns when the turn has been accepted (queued,
        sent), not when it has finished. Completion is observed via
        `events()` (`turn_end`).
        """
        ...

    # ----- output -----------------------------------------------------

    def events(self) -> AsyncIterator[AgentEvent]:
        """Yield `AgentEvent`s for the lifetime of the session.

        First event is conventionally `system_init` carrying the
        provider name and `capabilities` list (the orchestrator
        forwards this to subscribers). Last event is conventionally
        `session_end`, after which the iterator stops.

        Providers must not raise out of this iterator under normal
        operation — emit an `error` event instead. Raising is reserved
        for genuinely unrecoverable conditions where the iterator
        cannot continue.
        """
        ...

    # ----- optional capabilities --------------------------------------
    #
    # Methods below are part of the protocol but only meaningful when
    # the corresponding capability flag is advertised. Providers that
    # don't support them should still implement them as
    # NotImplementedError raisers, so a static type check passes; the
    # orchestrator never calls them when the flag is absent.

    async def set_model(self, model: str) -> None:
        """Change the active model for subsequent turns.

        Only meaningful with `Capability.MODEL_SWITCHING`. May be a
        no-op if the provider already routes per-call (OpenCode does);
        in that case the provider stores the choice and applies it on
        the next `send()`.
        """
        ...

    async def list_models(self) -> list[dict]:
        """Return the catalogue of models this provider can route to.

        Each entry is a dict with at least `value` (canonical model
        identifier the orchestrator passes back to `set_model`) and
        `label` (display name). Optional fields: `provider_id`,
        `model_id`, `description`, `default` (bool — pre-selected
        in UI), `current` (bool — currently active for this
        session).

        Only meaningful with `Capability.MODEL_SWITCHING`. Providers
        without dynamic routing should still return their static list
        (Claude returns its model family). Providers with no choice
        return an empty list.
        """
        ...

    async def set_agent(self, agent: str) -> None:
        """Change the active agent / mode for subsequent turns.

        Only meaningful with `Capability.AGENT_SWITCHING`.
        """
        ...

    async def get_context_usage(self) -> dict:
        """Report current context window usage.

        Only meaningful with `Capability.CONTEXT_USAGE`. Shape is
        provider-specific; the frontend's context-ring widget is
        currently Claude-shaped and a future generalization may
        constrain this further.
        """
        ...

    async def respond_to_permission(
        self,
        request_id: str,
        decision: str,
    ) -> None:
        """Reply to a permission request the provider previously
        emitted via a `permission_request` event.

        `decision` is forwarded verbatim to the underlying SDK; valid
        values are provider-specific:

          - OpenCode: "once" / "always" / "reject"
          - Claude (post-migration): "allow" / "deny" / similar

        The orchestrator translates frontend button clicks into
        provider-appropriate strings before calling this.
        """
        ...
