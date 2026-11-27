// The task text handed to page-agent.
//
// The agent runs on the same configured provider as the rest of the app, but it is a general
// model with DOM tools: the only thing keeping it truthful is that the profile is the whole
// input, plus explicit rules. Never auto-submit is enforced in the DOM by fill/guard.ts; the
// instruction not to click submit is a second layer, not the enforcement.

import type { Profile } from './types'

export const AGENT_SYSTEM = [
  'You fill job-application forms on the current page using ONLY the candidate profile provided',
  'in the task. Copy profile facts verbatim; never invent, guess, or pad a value that is not',
  'there. Do not click any button or link labelled Submit, Apply, Next, Continue, Save, Send, or',
  'Proceed. Do not navigate away or open new tabs. Skip file-upload fields. Never answer a captcha,',
  'spam check or "are you human" question, and never tick a consent, terms or privacy box: the candidate',
  'does those. If a field has no',
  'matching profile fact, leave it empty and move on. When the visible fields on this page are',
  'done, call the done tool with a one-line summary.',
].join(' ')

export function fillTask(profile: Profile): string {
  return [
    'Fill out the visible job application form fields on this page using ONLY the candidate profile below.',
    'Rules:',
    '1) Do not click any button labelled Submit, Apply, Next, Continue, Save, Send, or Proceed.',
    '2) Skip file-upload fields; the resume is attached separately.',
    '3) If a required field has no matching profile data, leave it and continue.',
    '4) For dropdowns and radio groups, pick the option that best matches the profile.',
    '5) Leave captchas, spam checks ("What is 4 + 3?") and consent/terms/privacy checkboxes for the candidate.',
    '6) Stop when all fillable fields on the current page are done.',
    '',
    'CANDIDATE PROFILE:',
    profileProse(profile),
  ].join('\n')
}

/** Flatten the profile into compact prose. Only stored facts appear — nothing is derived. */
export function profileProse(profile: Profile): string {
  const id = profile.identity ?? ({} as Profile['identity'])
  const lines: string[] = []

  const identity: [string, unknown][] = [
    ['Name', id.name],
    ['Email', id.email],
    ['Phone', id.phone],
    ['Location', (id.locations ?? []).join(' | ')],
    ['Links', (id.links ?? []).join(', ')],
    ['Citizenship', id.citizenship],
    ['Work authorization', id.work_authorization],
    ['Willing to relocate', id.willing_to_relocate === true ? 'Yes' : id.willing_to_relocate === false ? 'No' : ''],
  ]
  for (const [label, value] of identity) {
    const text = stringify(value)
    if (text) lines.push(`${label}: ${text}`)
  }

  const roles = (profile.roles ?? []).filter(Boolean)
  if (roles.length) lines.push(`Target roles: ${roles.join(', ')}`)

  const skills = (profile.skills ?? []).map((s) => s?.name).filter(Boolean)
  if (skills.length) lines.push(`Skills: ${skills.join(', ')}`)

  const experience = renderEntries(profile.experience)
  if (experience) lines.push('Experience:', experience)

  const projects = renderProjects(profile)
  if (projects) lines.push('Projects:', projects)

  const education = renderEntries(profile.education)
  if (education) lines.push('Education:', education)

  const certifications = renderEntries(profile.certifications)
  if (certifications) lines.push('Certifications:', certifications)

  const publications = renderEntries(profile.publications)
  if (publications) lines.push('Publications:', publications)

  const prefs = profile.preferences
  if (prefs) {
    const bits = [
      prefs.remote_only ? 'remote only' : '',
      prefs.comp_min ? `minimum comp ${prefs.comp_min}${prefs.comp_currency ? ` ${prefs.comp_currency}` : ''}` : '',
      prefs.seniority_floor ? `seniority ${prefs.seniority_floor}` : '',
      prefs.deal_breakers?.length ? `deal-breakers: ${prefs.deal_breakers.join(', ')}` : '',
    ].filter(Boolean)
    if (bits.length) lines.push(`Preferences: ${bits.join('; ')}`)
  }

  const anything = stringify(profile.anything_else)
  if (anything) lines.push(`Anything else: ${anything}`)

  return lines.join('\n')
}

function renderProjects(profile: Profile): string {
  const blocks: string[] = []
  for (const project of profile.projects ?? []) {
    if (!project?.title) continue
    const head = project.skills?.length ? `${project.title} (${project.skills.join(', ')})` : project.title
    blocks.push(renderBlock(head, project.bullets))
  }
  return blocks.join('\n')
}

function renderEntries(entries: unknown): string {
  if (!Array.isArray(entries)) return ''
  const blocks: string[] = []
  for (const entry of entries) {
    if (typeof entry === 'string') {
      blocks.push(`- ${entry}`)
      continue
    }
    if (!entry || typeof entry !== 'object') continue
    const record = entry as Record<string, unknown>
    const head = stringify(record.title ?? record.name ?? record.role ?? record.degree)
    const company = stringify(record.company ?? record.organization ?? record.institution)
    const dates = stringify(record.dates ?? record.period ?? record.year)
    const facts = [company, dates].filter(Boolean).join(' · ')
    const label = [head, facts].filter(Boolean).join(' — ') || 'Details'
    const bullets = Array.isArray(record.bullets) ? record.bullets : []
    blocks.push(renderBlock(label, bullets))
  }
  return blocks.join('\n')
}

function renderBlock(head: string, bullets: unknown[]): string {
  const lines = [`- ${head}`]
  for (const bullet of bullets ?? []) {
    const text = stringify(bullet)
    if (text) lines.push(`  • ${text}`)
  }
  return lines.join('\n')
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}
