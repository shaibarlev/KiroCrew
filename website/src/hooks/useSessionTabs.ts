import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import {
  closeSessionTab,
  loadSessionTabs,
  nextActiveAfterClose,
  openSessionTab,
  pruneSessionTabs,
  replaceSessionTab,
  saveSessionTabs,
} from '../lib/sessionTabs'

export interface SessionTabsApi {
  /** Open sessions in strip order. Length < 2 means the strip does not render. */
  tabs: string[]
  /** Open `key` beside the active tab. The caller still activates it. */
  openInNewTab: (key: string) => void
  /** Close `key`; returns the session to activate, or null when none is left. */
  closeTab: (key: string) => string | null
}

/** Stable identity so a disabled surface never re-renders on a new empty array. */
const EMPTY_TABS: string[] = []

/**
 * Owns the session-tab working set for one chat surface: restores it, keeps it
 * consistent with the active session and the live slot list, and persists it.
 *
 * The set is NOT in Redux. It is per-surface view state with a single consumer
 * tree (the chat page renders both the strip and the sidebar that feeds it), so
 * a slice would add a global mutation seam for something no other surface may
 * read — and `useSessionGrid`/`splitLayoutStore` already establish local state
 * plus a localStorage key as the pattern for chat-surface layout.
 *
 * INVARIANT: the active session is always in `tabs`. That is what makes the
 * feature invisible until it is used — a user who never opens a second tab has
 * a one-element set, and the strip renders nothing at all.
 *
 * `enabled=false` makes the whole hook INERT: no restore, no reconcile, no
 * write, and the mutators are no-ops. That is not an optimisation. ChatPage is
 * mounted by several EMBEDDED hosts too — a popped-out window, the artifact
 * companion panel, Papyrus's co-author panel, the app-SDK chat panel — and they
 * share one origin, therefore one `localStorage`. Left live, each of those
 * instances would reconcile the SAME key against its own active session and
 * overwrite the dashboard's working set with a session the dashboard never
 * opened (or, for an app-owned slot, one its sidebar cannot even show). One
 * predicate has to decide both who draws the strip and who owns the set, or the
 * two drift apart; see `ownsSessionTabs` in ChatPage.
 */
export function useSessionTabs(
  mode: string | undefined,
  activeSlot: string | null,
  /** Sessions living on this surface. Must be referentially stable (memoized). */
  liveSlots: readonly { key: string }[],
  /** Whether this surface owns the working set. False on every embedded host. */
  enabled: boolean,
): SessionTabsApi {
  const [tabs, setTabs] = useState<string[]>(() => (enabled ? loadSessionTabs(mode, activeSlot) : []))
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const activeRef = useRef(activeSlot)
  activeRef.current = activeSlot
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  // The session the user was on BEFORE this change — the tab whose content a
  // plain sidebar click replaces. Seeded from the mount value so the first
  // switch of a visit replaces rather than appends.
  const prevActiveRef = useRef<string | null>(activeSlot)

  useEffect(() => { if (enabled) saveSessionTabs(mode, tabs, safeSetItem) }, [enabled, mode, tabs])

  const liveKeys = useMemo(() => new Set(liveSlots.map(s => s.key)), [liveSlots])

  // A session that was deleted, or moved to another surface, cannot stay a tab.
  // Skipped while the list is empty: on a cold load the slots arrive after the
  // first paint, and pruning against nothing would discard the restored set
  // before it could ever be shown.
  useEffect(() => {
    if (!enabled || liveKeys.size === 0) return
    setTabs(prev => {
      const next = pruneSessionTabs(prev, liveKeys)
      return next.length === prev.length ? prev : next
    })
  }, [enabled, liveKeys])

  // Keep the invariant. An activation that is already a tab is just a
  // selection (the strip derives "active" from activeSlot, so there is nothing
  // to store); one that is not takes over the tab the user was looking at.
  useEffect(() => {
    const prev = prevActiveRef.current
    prevActiveRef.current = activeSlot
    if (!enabled || !activeSlot) return
    setTabs(current => {
      if (current.includes(activeSlot)) return current
      if (current.length === 0) return [activeSlot]
      return replaceSessionTab(current, prev, activeSlot)
    })
  }, [enabled, activeSlot])

  const openInNewTab = useCallback((key: string) => {
    if (!enabledRef.current) return
    setTabs(current => openSessionTab(current, key, activeRef.current))
  }, [])

  const closeTab = useCallback((key: string) => {
    if (!enabledRef.current) return null
    const current = tabsRef.current
    const nextActive = nextActiveAfterClose(current, key, activeRef.current)
    setTabs(closeSessionTab(current, key))
    return nextActive
  }, [])

  return { tabs: enabled ? tabs : EMPTY_TABS, openInNewTab, closeTab }
}
