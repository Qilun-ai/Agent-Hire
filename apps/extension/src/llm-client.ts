// The content script's fetch adapter for page-agent.
//
// page-agent sends OpenAI chat-completions requests with `fetch`; a career page's connect-src
// would block (or CORS-refuse) a direct call to 127.0.0.1, so this adapter relays each request
// through the extension worker to the backend's tool proxy. The URL page-agent built is ignored
// on purpose: the only endpoint reachable this way is the fixed backend origin, which holds the
// provider key. The page never sees a model key.

import type { LlmProxyResponse } from './shared/messages'

export async function backgroundFetch(_input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const body = typeof init?.body === 'string' ? init.body : ''
  const relayed = (await chrome.runtime.sendMessage({
    type: 'LLM_PROXY',
    body,
  })) as LlmProxyResponse | undefined

  if (!relayed) {
    return jsonResponse(502, 'Bad Gateway', {
      error: { message: 'LLM relay returned no response from the extension worker' },
    })
  }
  if (!relayed.ok) {
    // page-agent reports this only as "HTTP 400: Bad Request"; tell the panel the server's reason
    // (no model configured, provider down, model without tool calling, ...).
    void chrome.runtime.sendMessage({ type: 'AGENT_STATUS', text: `AI model error: ${relayDetail(relayed.body)}` })
  }
  return jsonResponse(relayed.status || 502, relayed.statusText, relayed.body, true)
}

export function relayDetail(body: string): string {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown; error?: { message?: unknown } }
    if (typeof parsed.detail === 'string') return parsed.detail
    if (typeof parsed.error?.message === 'string') return parsed.error.message
  } catch {
    /* not JSON */
  }
  return body.slice(0, 200) || 'no response'
}

function jsonResponse(status: number, statusText: string, body: string | object, raw = false): Response {
  const payload = raw ? String(body) : typeof body === 'string' ? body : JSON.stringify(body)
  return new Response(payload, {
    status,
    statusText,
    headers: { 'content-type': 'application/json' },
  })
}
