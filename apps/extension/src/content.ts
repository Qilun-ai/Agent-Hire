// Content script: a small listener in every frame. The agent itself (page-agent, ~200 KB) lives in
// fill/run.ts and is imported only when the user fills this frame, so ordinary browsing pays
// nothing. The background worker can also inject this file into tabs opened before the extension
// was installed or reloaded, so the listener guards against being registered twice.

import type { ExtensionMessage } from './shared/messages'

const flagHost = globalThis as unknown as { __agentHireFillListener?: boolean }

if (!flagHost.__agentHireFillListener) {
  flagHost.__agentHireFillListener = true
  let running = false

  chrome.runtime.onMessage.addListener((msg: ExtensionMessage, _sender, sendResponse) => {
    if (msg.type === 'STOP_FILL') {
      if (running) void import('./fill/run').then((m) => m.stopFill())
      return false
    }
    if (msg.type !== 'FILL_PAGE') return false
    if (running) {
      sendResponse({ ack: false, error: 'A fill is already running on this page.' })
      return false
    }
    running = true
    sendResponse({ ack: true })
    void import('./fill/run')
      .then((m) => m.runFill(msg))
      .catch((e: unknown) =>
        chrome.runtime.sendMessage({
          type: 'AGENT_ERROR',
          message: `Could not start the filler: ${e instanceof Error ? e.message : String(e)}`,
        }),
      )
      .finally(() => {
        running = false
      })
    return false
  })
}

export {}
