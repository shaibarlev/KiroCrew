/**
 * ChatPage ↔ session-tab strip wiring (#4477).
 *
 * The two facts that only a page-level render can pin:
 *   1. On a normal visit the strip is NOT in the tree at all. Every unit test
 *      of SessionTabStrip could pass while ChatPage mounted it unconditionally,
 *      which would move the transcript down for every user of the surface.
 *   2. The sidebar is handed a working `onOpenSlotInNewTab`, and using it is
 *      what makes the strip appear. A callback that never reached the sidebar
 *      would leave the whole feature unreachable with no test failing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Capture the props ChatPage hands the sidebar; render nothing, so the assertions
// are about ChatPage's own tree.
const sidebarProps: Array<{ onOpenSlotInNewTab?: (key: string) => void }> = []
vi.mock('../pages/ChatSidebar', () => ({
  default: (props: { onOpenSlotInNewTab?: (key: string) => void }) => {
    sidebarProps.push({ onOpenSlotInNewTab: props.onOpenSlotInNewTab })
    return null
  },
  SIDEBAR_MIN: 200,
  SIDEBAR_MAX: 500,
}))

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
        )
      }
      return apiMocks[prop]
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never

import ChatPage from '../pages/ChatPage'

const SLOTS = [
  { key: 'chat-1', title: 'First', messages: 1, running: false, mode: '', created: '', last_ts: '' },
  { key: 'chat-2', title: 'Second', messages: 1, running: false, mode: '', created: '', last_ts: '' },
]

function renderChatPage(props: { embedded?: boolean; embedMode?: 'chat' | 'sessions' } = {}) {
  apiMocks.chatSlots = vi.fn().mockResolvedValue(SLOTS)
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' }, connected: false,
      slots: SLOTS,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" {...props} />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatPage – session tab strip', () => {
  beforeEach(() => {
    sidebarProps.length = 0
    localStorage.clear()
  })

  it('does NOT mount the strip on a normal visit', () => {
    renderChatPage()
    expect(screen.queryByTestId('session-tab-strip')).toBeNull()
    expect(screen.queryByRole('tablist')).toBeNull()
  })

  it('hands the sidebar a usable open-in-new-tab callback', () => {
    renderChatPage()
    expect(typeof sidebarProps.at(-1)?.onOpenSlotInNewTab).toBe('function')
  })

  it('shows the strip once a second session is opened as a tab', () => {
    renderChatPage()
    const open = sidebarProps.at(-1)?.onOpenSlotInNewTab
    act(() => open?.('chat-2'))
    expect(screen.getByTestId('session-tab-strip')).toBeTruthy()
    expect(screen.getByTestId('session-tab-chat-1')).toBeTruthy()
    expect(screen.getByTestId('session-tab-chat-2')).toBeTruthy()
  })

  it('restores a persisted working set on the next visit', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    renderChatPage()
    expect(screen.getByTestId('session-tab-strip')).toBeTruthy()
    expect(screen.getAllByRole('tab')).toHaveLength(2)
  })

  it('hides the strip again when the set drops back to one tab', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    renderChatPage()
    act(() => { screen.getByLabelText('Close tab Second').click() })
    expect(screen.queryByTestId('session-tab-strip')).toBeNull()
  })

  // An EMBEDDED ChatPage — popped-out window, artifact companion panel, Papyrus
  // co-author panel, app-SDK chat panel — must neither draw the strip nor touch
  // the persisted set: it runs on the dashboard's origin and would otherwise
  // reconcile the same localStorage key against its own active session.
  it('draws no strip on an embedded host, even with a persisted set', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    renderChatPage({ embedded: true })
    expect(screen.queryByTestId('session-tab-strip')).toBeNull()
  })

  it('leaves the persisted set untouched from an embedded host', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    renderChatPage({ embedded: true, embedMode: 'chat' })
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-1","chat-2"]')
  })

  it('offers no open-in-new-tab gesture from an embedded host', () => {
    // The sidebar would otherwise bind a middle-click that writes into a set
    // this surface cannot show.
    sidebarProps.length = 0
    renderChatPage({ embedded: true })
    if (sidebarProps.length) expect(sidebarProps.at(-1)?.onOpenSlotInNewTab).toBeUndefined()
  })
})
