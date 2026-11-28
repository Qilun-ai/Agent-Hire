// The agent fill, loaded on demand by content.ts so the page-agent bundle is only fetched into a
// page when the user actually fills it (not into every frame of every site they visit):
//   submit guard on → page-agent observe/act loop (LLM calls relayed to the backend) → attach resume.
//
// The agent gets the candidate profile in the task and makes every value decision through the
// backend's tool-calling proxy, so this file never talks to a model and holds no key. It never
// clicks Submit/Apply/Next — a capture-phase guard drops untrusted clicks on those for the run.

import { PageAgent, type AgentActivity } from 'page-agent'

import type { FillPage } from '../shared/messages'
import { installSubmitGuard } from './guard'
import { BACKEND_URL } from '../shared/backend'
import { resumeFileName, tryAttachResume } from './resume'
import { runDecisionFill } from './decide'
import { backgroundFetch } from '../llm-client'
import { AGENT_SYSTEM, fillTask } from '../shared/agentTask'

let agent: PageAgent | null = null
let stopped = false

/** Stop the running agent; the fill ends with whatever it already typed. */
export function stopFill(): void {
  stopped = true
  void agent?.stop()
}

/** Default: the decision-model filler. mode "agent" runs the LLM page agent (the fallback button). */
export async function runFill(msg: FillPage): Promise<void> {
  return msg.mode === 'agent' ? runAgentFill(msg) : runDecisionFill(msg)
}

const status = (text: string) => void chrome.runtime.sendMessage({ type: 'AGENT_STATUS', text })
const done = (success: boolean, summary: string) =>
  void chrome.runtime.sendMessage({ type: 'AGENT_DONE', success, summary })

async function runAgentFill(msg: FillPage): Promise<void> {
  stopped = false
  const releaseGuard = installSubmitGuard(status)
  try {
    const before = snapshotFields()
    if (!before.size) {
      done(false, 'No fillable fields found on this page.')
      return
    }
    status(`Agent scanning ${before.size} field${before.size === 1 ? '' : 's'}…`)

    // The base URL is a placeholder: backgroundFetch relays every call to the fixed backend
    // origin, so the agent can never reach an arbitrary model endpoint.
    agent = new PageAgent({
      model: 'agent-hire-backend',
      baseURL: `${BACKEND_URL}/api/v1/llm`,
      apiKey: 'backend-managed',
      language: 'en-US',
      // A 20-field page costs roughly one step per field plus overhead.
      maxSteps: 60,
      // Weaker models intermittently answer with prose instead of a tool call; one bad step
      // should not end a run that is fifteen fields deep.
      maxRetries: 5,
      // Never block the run waiting for a human to type into the in-page panel; the task tells
      // the agent to skip anything it cannot answer.
      customTools: { ask_user: null },
      transformRequestBody: (body: Record<string, unknown>) => {
        // The proxy parses one JSON body, so a streaming reply would read as empty.
        body.stream = false
        return body
      },
      customFetch: backgroundFetch,
      instructions: { system: AGENT_SYSTEM },
    })

    agent.addEventListener('activity', (event) => {
      const text = describeActivity((event as CustomEvent<AgentActivity>).detail)
      if (text) status(text)
    })

    const result = await agent.execute(fillTask(msg.profile))
    const filled = countChanged(before)

    // Attach the tailored resume after the agent is done with the form (uploads are skipped).
    if (msg.resumeUrl && !stopped) await tryAttachResume(msg.resumeUrl, resumeFileName(msg.profile.identity?.name), status)
    else if (!msg.resumeUrl) status('No tailored résumé for this job yet — upload one yourself if the form asks.')

    // Report the outcome so it lands in the activity log (§6). Fire-and-forget: the fill already
    // happened, and a failed audit write must not turn a successful fill into an error.
    void chrome.runtime.sendMessage({
      type: 'FILL_REPORT',
      jobId: msg.jobId ?? null,
      filled,
      total: before.size,
    })

    const counted = `Filled ${filled} of ${before.size} field${before.size === 1 ? '' : 's'}.`
    done(
      result.success || filled > 0,
      stopped
        ? `Stopped. ${counted}`
        : filled > 0
          ? `${counted} Review everything, then click Submit yourself. On a multi-page form, go to the next page and fill again.`
          : result.data || 'The agent finished without changing any field — check the log.',
    )
  } catch (e) {
    void chrome.runtime.sendMessage({
      type: 'AGENT_ERROR',
      message: e instanceof Error ? e.message : String(e),
    })
  } finally {
    releaseGuard()
    agent?.dispose()
    agent = null
  }
}

/** page-agent emits transient activity events; turn them into one log line each. */
function describeActivity(activity: AgentActivity): string | null {
  switch (activity.type) {
    case 'thinking':
      return 'Thinking…'
    case 'executing':
      return describeTool(activity.tool, activity.input)
    case 'executed':
      return null // the 'executing' line already covered it
    case 'retrying':
      return `Retrying (${activity.attempt}/${activity.maxAttempts})…`
    case 'error':
      return `Problem: ${activity.message}`
    default:
      return null
  }
}

function describeTool(tool: string, input: unknown): string {
  const args = (input ?? {}) as Record<string, unknown>
  const index = typeof args.index === 'number' ? ` [${args.index}]` : ''
  switch (tool) {
    case 'input_text':
      return `Typing${index}: ${truncate(typeof args.text === 'string' ? args.text : '', 60)}`
    case 'select_dropdown_option':
      return `Selecting${index}: ${truncate(typeof args.text === 'string' ? args.text : '', 60)}`
    case 'click_element_by_index':
      return `Clicking element${index}`
    case 'scroll':
    case 'scroll_horizontally':
      return 'Scrolling…'
    case 'wait':
      return 'Waiting…'
    case 'done':
      return 'Wrapping up…'
    default:
      return `Running ${tool}…`
  }
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value
}

// --- field snapshot (for the fill report) ------------------------------------
// The agent owns the DOM writes; we only measure what changed so the activity log can say
// "filled 9 of 14". The element set mirrors the worker's richest-frame scan.

function snapshotFields(): Map<HTMLElement, string> {
  const map = new Map<HTMLElement, string>()
  for (const el of document.querySelectorAll<HTMLElement>('input, select, textarea')) {
    if (!isFillable(el)) continue
    map.set(el, readValue(el))
  }
  return map
}

function countChanged(before: Map<HTMLElement, string>): number {
  let filled = 0
  for (const [el, previous] of before) {
    const current = readValue(el)
    if (current !== previous && current.trim() !== '') filled++
  }
  return filled
}

function readValue(el: HTMLElement): string {
  if (el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio')) {
    return el.checked ? 'on' : 'off'
  }
  if (el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement) {
    return el.value
  }
  return ''
}

function isFillable(el: HTMLElement): boolean {
  if (el instanceof HTMLInputElement) {
    const type = el.type.toLowerCase()
    if (['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password'].includes(type)) return false
    if (el.disabled) return false
  } else if ((el as HTMLSelectElement | HTMLTextAreaElement).disabled) {
    return false
  }
  const rect = el.getBoundingClientRect()
  if (rect.width === 0 && rect.height === 0) return false
  const style = getComputedStyle(el)
  return style.visibility !== 'hidden' && style.display !== 'none'
}
