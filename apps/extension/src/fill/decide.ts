// SPDX-License-Identifier: AGPL-3.0-only
// The default fill: read the form, ask the backend which saved value answers each field (rules, then
// the decision model, then the main AI model for what needs writing), and type those values in.
// No model runs in the page and nothing is clicked except radio buttons and non-consent checkboxes;
// the submit guard stays on for the whole run.

import type { FillPage } from '../shared/messages'
import { pageBackendJson } from '../shared/backend'
import { installSubmitGuard } from './guard'
import { applyFills } from './apply'
import { harvestComboOptions, scanFields, type ScannedField } from './scan'
import { resumeFileName, tryAttachResume } from './resume'

interface PlanFill {
  id: string
  value: string
  key: string
  source: string
  from: string
}

interface Plan {
  fills: PlanFill[]
  skipped: { id: string; reason: string }[]
  decided_by: string
  job_data?: { source?: string | null; confidence?: number | null }
}

const status = (text: string) => void chrome.runtime.sendMessage({ type: 'AGENT_STATUS', text })
const done = (success: boolean, summary: string) => void chrome.runtime.sendMessage({ type: 'AGENT_DONE', success, summary })

const DECIDED_BY: Record<string, string> = {
  rules: 'built-in rules',
  decision_model: 'the decision model',
  'decision_model+main_model': 'the decision model, with the main AI model for written answers',
  main_model: 'the main AI model (no decision model was reachable)',
}

const short = (text: string, max = 48) => (text.length > max ? `${text.slice(0, max)}…` : text)

export async function runDecisionFill(msg: FillPage): Promise<void> {
  const releaseGuard = installSubmitGuard(status)
  try {
    const { fields, targets } = scanFields()
    if (!fields.length) {
      done(false, 'No fillable fields found on this page.')
      return
    }
    await harvestComboOptions(fields, targets)
    status(`Read ${fields.length} field${fields.length === 1 ? '' : 's'} — choosing values…`)
    const plan = (await pageBackendJson('/api/v1/fill/plan', { job_id: msg.jobId ?? '', fields })) as Plan
    const byId = new Map<string, ScannedField>(fields.map((f) => [f.id, f]))
    const name = (id: string) => short(byId.get(id)?.label || byId.get(id)?.placeholder || byId.get(id)?.name || id)

    const result = await applyFills(plan.fills, targets)
    for (const id of result.filled) {
      const fill = plan.fills.find((f) => f.id === id)
      if (fill) status(`✓ ${name(id)}: ${short(fill.value.replace(/\s+/g, ' '), 40)}${fill.key === 'WRITE' ? ' (written by AI)' : ''}`)
    }
    for (const f of result.failed) status(`✗ ${name(f.id)}: ${f.reason}`)
    const skipped = plan.skipped.filter((s) => !result.kept.includes(s.id))
    for (const s of skipped.slice(0, 12)) status(`– ${name(s.id)}: ${s.reason}`)
    if (skipped.length > 12) status(`– …and ${skipped.length - 12} more left for you`)

    if (msg.resumeUrl) await tryAttachResume(msg.resumeUrl, resumeFileName(msg.profile.identity?.name), status)
    else status('No tailored résumé for this job yet — upload one yourself if the form asks.')

    void chrome.runtime.sendMessage({ type: 'FILL_REPORT', jobId: msg.jobId ?? null, filled: result.filled.length, total: fields.length })

    const counted = `Filled ${result.filled.length} of ${fields.length} field${fields.length === 1 ? '' : 's'}`
    const kept = result.kept.length ? `, kept ${result.kept.length} you had already filled` : ''
    const by = DECIDED_BY[plan.decided_by] ?? plan.decided_by
    done(
      result.filled.length > 0 || result.kept.length > 0,
      `${counted}${kept} (values chosen by ${by}). Check every answer, then click Submit yourself. ` +
        (skipped.length ? `${skipped.length} left for you — or try “Fill with AI agent”.` : ''),
    )
  } catch (e) {
    void chrome.runtime.sendMessage({ type: 'AGENT_ERROR', message: e instanceof Error ? e.message : String(e) })
  } finally {
    releaseGuard()
  }
}
