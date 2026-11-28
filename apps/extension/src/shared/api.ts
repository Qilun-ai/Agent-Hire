// Typed backend client for the side panel (an extension page — it fetches the sidecar directly
// through shared/backend.ts). Everything decision-shaped lives on the backend.

import { BACKEND_URL, backendJson } from './backend'
import type { ExtensionJob, FillContext } from './types'

export { BackendError } from './backend'

/** Liveness: any 2xx means the app is up. (The API answers {"status": "alive"}; an older panel
 *  insisted on "ok" and so showed "offline" against a perfectly healthy app.) */
export function health(): Promise<{ status?: string }> {
  return backendJson<{ status?: string }>('/health')
}

/** The website's address — the API serves the built UI on the same origin. */
export function appUrl(): string {
  return BACKEND_URL
}

/** Cart jobs first, then jobs already prepared for applying. Finished jobs are left out. */
export async function listJobs(): Promise<ExtensionJob[]> {
  const body = await backendJson<{ jobs?: ExtensionJob[] }>('/api/v1/extension/jobs')
  return Array.isArray(body.jobs) ? body.jobs : []
}

/** The fill payload for the job the user picked. Built from the profile when no prepared handoff exists. */
export function getFillContextForJob(jobId: string): Promise<FillContext> {
  return backendJson<FillContext>(`/api/v1/fill-context?job_id=${encodeURIComponent(jobId)}`)
}

/** The human confirms they submitted — the only way a job becomes "applied". */
export function markApplied(jobId: string): Promise<{ ok: boolean }> {
  return backendJson<{ ok: boolean }>(`/api/v1/leads/${encodeURIComponent(jobId)}/applied`, { method: 'POST' })
}

/** The per-job fill payload the backend prepared for the active tab (docs/11 §W1.5). */
export async function getFillContext(url: string): Promise<FillContext | null> {
  try {
    return await backendJson<FillContext>(`/api/v1/fill-context?url=${encodeURIComponent(url)}`)
  } catch (e) {
    if (e instanceof Error && 'status' in e && (e as { status: number }).status === 404) return null
    throw e
  }
}

/** One tool-calling round trip through the backend proxy.
 *
 *  The page agent drives forms entirely through function calls, so "the backend has a model"
 *  is not the same as "agent fill can run". This probe answers the second question before the
 *  user starts a fill, instead of the run dying on its first step. */
export async function checkAgentToolCalling(): Promise<{ ok: boolean; detail: string }> {
  const body = {
    model: 'agent-hire-agent',
    messages: [{ role: 'user', content: 'Call the probe tool with ok=true.' }],
    tools: [
      {
        type: 'function',
        function: {
          name: 'probe',
          description: 'Readiness probe',
          parameters: {
            type: 'object',
            properties: { ok: { type: 'boolean' } },
            required: ['ok'],
          },
        },
      },
    ],
    tool_choice: 'required',
    stream: false,
  }
  try {
    const out = await backendJson<{
      choices?: { message?: { tool_calls?: { function?: { name?: string } }[] } }[]
    }>('/api/v1/llm/chat/completions', { method: 'POST', body: JSON.stringify(body) })
    const call = out.choices?.[0]?.message?.tool_calls?.[0]
    return call
      ? { ok: true, detail: 'The configured model answered with a tool call.' }
      : { ok: false, detail: 'The model replied without a tool call — pick a tool-calling model in Settings.' }
  } catch (e) {
    const detail = e instanceof Error ? e.message : String(e)
    return { ok: false, detail: detail.slice(0, 300) }
  }
}
