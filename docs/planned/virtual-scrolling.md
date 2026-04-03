# Virtual Scrolling + Simplified Replay for Agent View

## Problem

Sessions with 3000+ messages freeze the browser during history replay. Each message triggers full markdown parsing, DOM creation, event listener attachment, and group line recalculation — all synchronously. The chunked loading helps but the DOM still gets massive.

## Approach: Two-phase optimization

### Phase 1: Simplified replay rendering

During history replay (`_replayingHistory === true`), render messages as **minimal placeholders** instead of full DOM:

- **User messages**: plain text div, no collapsible logic, no View/Copy buttons
- **Assistant text**: plain text div, skip `_mdToHtml()` — just `escHtml()` with basic newline handling
- **Tool use blocks**: single-line summary only (icon + name + input summary), no `<details>`, no `_formatToolInput()`
- **Thinking blocks**: skip entirely during replay (collapsed anyway)
- **Tool results**: skip entirely during replay (only errors shown)
- **Questions/plans**: simplified inline text, no modals

After replay finishes and user scrolls to a message, **upgrade it** to full rendering on demand (lazy upgrade via IntersectionObserver).

**Key change**: `renderEvent()` checks `_replayingHistory` and calls a lightweight `_renderMinimal(event)` instead of the full rendering path.

### Phase 2: Virtual scrolling

Only keep ~50 messages in the DOM at a time (viewport + buffer above/below). As the user scrolls, recycle DOM nodes.

**Data model**:
- `_allEvents[]` — array of all raw event objects (kept in memory, lightweight)
- `_renderedRange = {start, end}` — indices of events currently in the DOM
- Each event gets a fixed estimated height (user msg: 60px, tool: 40px, text: variable)

**Scroll handler** (throttled to 16ms / rAF):
1. Calculate which events are visible based on `scrollTop` and estimated heights
2. Expand the range by 20 above and 20 below (buffer)
3. Remove DOM nodes outside the new range
4. Create DOM nodes for newly visible events (full render, not minimal)
5. Use a spacer div at top/bottom to maintain scroll height

**Challenges**:
- Variable message heights (text blocks can be huge)
- `msg-group` grouping — turns must stay together
- Group connector lines need recalculation
- Navigation (up/down arrows) needs to work with virtual DOM

**Mitigations**:
- After first render, cache actual heights per event index
- Groups are the virtual unit, not individual messages — render/remove entire groups
- Connector lines only calculated for visible groups
- Navigation scrolls to target index, which triggers virtual render

## Key files

- `be_conductor/static/agent-view.html` — all changes in this file

### Key functions to modify
- `renderEvent()` (line ~746) — add minimal render path
- `_mdToHtml()` (line ~546) — skip during replay
- `autoScroll()` (line ~1357) — work with virtual range
- `_updateGroupLines()` (line ~1313) — only visible groups
- History replay loop (line ~2074) — store events, render minimal

## Implementation order

1. Add `_allEvents[]` array — store every event during replay AND live
2. Add `_renderMinimal(event)` — lightweight placeholder renderer
3. Modify `renderEvent()` — during replay, call `_renderMinimal` instead
4. Add `_upgradeMessage(el, eventIdx)` — full render a minimal placeholder
5. Add IntersectionObserver to lazy-upgrade visible minimal messages
6. Add virtual scroll manager — `_updateVisibleRange()` on scroll
7. Add spacer divs for scroll height maintenance
8. Update navigation to work with virtual indices
9. Update `autoScroll()` to work with virtual range

## Expected results

- Load a 3000+ message session in <1 second
- Scroll through history — messages upgrade to full rendering as they enter viewport
- Navigation arrows work — jumping to old messages renders them
- Live messages still render fully and immediately
- Memory usage stays low regardless of session size
