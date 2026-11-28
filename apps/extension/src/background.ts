// Service worker. Opens the side panel; routes FILL_PAGE to the richest frame; relays backend
// calls for content scripts (the worker isn't bound by a page's connect-src CSP, so the local
// backend stays reachable — docs/06 §4). It holds the only copy of the backend token besides the
// panel; the extension never talks to a model directly.

import type {
  BackendBlobRequest,
  BackendBlobResponse,
  BackendJsonRequest,
  BackendJsonResponse,
  ExtensionMessage,
  FillAck,
  FillPage,
  FillReport,
  LlmProxyRequest,
  LlmProxyResponse,
} from './shared/messages'
import { backendFetch, backendJson } from './shared/backend'

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.warn)
})
chrome.action.onClicked.addListener(async (tab) => {
  if (tab.windowId != null) {
    try {
      await chrome.sidePanel.open({ windowId: tab.windowId })
    } catch (e) {
      console.warn('sidePanel open:', e)
    }
  }
})

chrome.runtime.onMessage.addListener((message: ExtensionMessage, _sender, sendResponse) => {
  switch (message.type) {
    case 'FILL_PAGE':
      forwardFillToBestFrame(message).then(sendResponse)
      return true
    case 'STOP_FILL':
      if (message.tabId != null) void chrome.tabs.sendMessage(message.tabId, message).catch(() => undefined)
      return false
    case 'BACKEND_JSON':
      relayJson(message).then(sendResponse)
      return true
    case 'BACKEND_BLOB':
      relayBlob(message).then(sendResponse)
      return true
    case 'LLM_PROXY':
      relayLlm(message).then(sendResponse)
      return true
    case 'FILL_REPORT':
      // Forwarded from here rather than sent by the content script: the page's origin has no
      // permission for the backend, so a fetch from there would be blocked by CORS.
      void reportFill(message)
      return false
    default:
      // AGENT_STATUS / AGENT_DONE / AGENT_ERROR are broadcast to the panel directly.
      return false
  }
})

/** Relayed content-script call. Only relative paths reach the fixed backend origin. */
async function relayJson(message: BackendJsonRequest): Promise<BackendJsonResponse> {
  try {
    const resp = await backendFetch(safePath(message.path), {
      method: message.init?.method ?? 'GET',
      body: message.init?.body,
    })
    return { ok: resp.ok, status: resp.status, body: await resp.text() }
  } catch (e) {
    return { ok: false, status: 0, body: e instanceof Error ? e.message : String(e) }
  }
}

async function relayBlob(message: BackendBlobRequest): Promise<BackendBlobResponse> {
  try {
    const resp = await backendFetch(safePath(message.path))
    if (!resp.ok) return { ok: false, status: resp.status }
    const bytes = new Uint8Array(await resp.arrayBuffer())
    let binary = ''
    const CHUNK = 0x8000
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK))
    }
    return { ok: true, status: resp.status, base64: btoa(binary) }
  } catch {
    return { ok: false, status: 0 }
  }
}

function safePath(path: string): string {
  if (!path.startsWith('/') || path.startsWith('//')) throw new Error('relay: relative backend path required')
  return path
}

/**
 * Relay one page-agent chat-completions call to the backend's tool proxy.
 *
 * The destination is hardcoded: page-agent's own base URL is deliberately ignored, because the
 * only model endpoint a page may reach is the one this worker is configured to talk to. The body
 * is forwarded verbatim; the backend resolves provider, key, and model.
 */
async function relayLlm(message: LlmProxyRequest): Promise<LlmProxyResponse> {
  try {
    const resp = await backendFetch('/api/v1/llm/chat/completions', { method: 'POST', body: message.body })
    return { ok: resp.ok, status: resp.status, statusText: resp.statusText, body: await resp.text() }
  } catch (e) {
    const detail = e instanceof Error ? e.message : String(e)
    return {
      ok: false,
      status: 0,
      statusText: 'relay failed',
      body: JSON.stringify({ error: { message: `LLM relay failed: ${detail}` } }),
    }
  }
}

/**
 * Tell the backend what the fill achieved, for the activity log (§6).
 *
 * Deliberately best-effort and unawaited by the caller: the fill has already happened, so failing to
 * record it must not surface as an error on a successful fill. A missing jobId means the user filled
 * a page with no prepared application, which is nothing to report.
 */
async function reportFill(message: FillReport): Promise<void> {
  if (!message.jobId) return
  try {
    await backendJson(
      `/api/v1/leads/${encodeURIComponent(message.jobId)}/filled`,
      {
        method: 'POST',
        body: JSON.stringify({
          filled: Array.from({ length: message.filled }, (_, i) => `field-${i + 1}`),
          skipped: Array.from({ length: Math.max(0, message.total - message.filled) }, (_, i) => `unfilled-${i + 1}`),
        }),
      },
    )
  } catch {
    /* the activity log is not worth failing a completed fill over */
  }
}

// --- FILL_PAGE: pick exactly one frame (docs/06 §3) --------------------------

async function forwardFillToBestFrame(message: FillPage): Promise<FillAck> {
  const tab =
    message.tabId != null
      ? await chrome.tabs.get(message.tabId).catch(() => null)
      : (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0]
  if (!tab?.id) return { ack: false, error: 'No active tab.' }
  if (isRestrictedUrl(tab.url)) {
    return { ack: false, error: 'Chrome blocks extensions on this page. Open the job application in a normal tab.' }
  }

  let frameId = 0
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: countFillableFields,
    })
    const best = results
      .filter((r) => typeof r.result === 'number' && (r.result as number) > 0)
      .sort((a, b) => (b.result as number) - (a.result as number))[0]
    if (!best) {
      return {
        ack: false,
        error: 'No form fields on this page yet. Click the site\'s Apply button (sign in if asked) until the form shows, then fill again.',
      }
    }
    frameId = best.frameId
  } catch (e) {
    const detail = errorText(e)
    if (/Cannot access|manifest must request permission|permission/i.test(detail)) {
      return {
        ack: false,
        error: 'Agent-Hire is not allowed on this site. Click the extension icon → "This can read and change site data" → "On all sites", then fill again.',
      }
    }
    return { ack: false, error: `Cannot access this page: ${detail}` }
  }

  const send = () => chrome.tabs.sendMessage(tab.id!, message, { frameId }) as Promise<FillAck | undefined>
  try {
    return (await send()) ?? { ack: true }
  } catch {
    // Tabs opened before the extension was installed or reloaded have no content script yet.
    try {
      await injectContentScript(tab.id, frameId)
      return (await send()) ?? { ack: true }
    } catch (e) {
      return { ack: false, error: `Could not start the filler on this page — reload the page and try again (${errorText(e)}).` }
    }
  }
}

/** Inject the manifest's content script into one frame (same file Chrome injects on page load). */
async function injectContentScript(tabId: number, frameId: number): Promise<void> {
  const files = chrome.runtime.getManifest().content_scripts?.flatMap((entry) => entry.js ?? []) ?? []
  if (!files.length) throw new Error('no content script in the manifest')
  await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files })
}

/** Injected into every frame; must be self-contained. */
function countFillableFields(): number {
  const nodes = document.querySelectorAll<HTMLElement>('input, select, textarea')
  let count = 0
  for (const el of nodes) {
    if (el instanceof HTMLInputElement) {
      const t = el.type.toLowerCase()
      if (t === 'hidden' || t === 'submit' || t === 'button' || t === 'file') continue
    }
    if ((el as HTMLInputElement).disabled) continue
    const rect = el.getBoundingClientRect()
    if (rect.width === 0 && rect.height === 0) continue
    const style = getComputedStyle(el)
    if (style.visibility === 'hidden' || style.display === 'none') continue
    count++
  }
  return count
}

function isRestrictedUrl(url: string | undefined): boolean {
  if (!url) return true
  return (
    /^(chrome|edge|brave|about|devtools|chrome-extension|view-source):/i.test(url) ||
    url.startsWith('https://chromewebstore.google.com')
  )
}
function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export {}
