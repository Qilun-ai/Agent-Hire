// End-to-end: load the built extension into Chromium, open a local job-application page, start a fill
// exactly as the side panel's "Fill this page" button does (FILL_PAGE through the service worker), record every
// status line the content script broadcasts, then read back what landed in each field.
//
//   node run.mjs <extension-dist> <page-url> <job-id> <out.json> [decision|agent] [chrome-executable]
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(process.env.PW_FROM || import.meta.url)
const { chromium } = require('playwright')
const [dist, pageUrl, jobId, out, mode = 'decision', exe] = process.argv.slice(2)
const API = 'http://127.0.0.1:8000'

const profileDir = fs.mkdtempSync(path.join(process.env.E2E_TMP || os.tmpdir(), 'gh-e2e-profile-'))
const ctx = await chromium.launchPersistentContext(profileDir, {
  headless: true,
  executablePath: exe || undefined,
  channel: exe ? undefined : 'chromium', // full Chromium in new-headless mode: the headless shell has no extensions
  args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, '--disable-gpu'],
})
try {
  const sw = ctx.serviceWorkers()[0] ?? (await ctx.waitForEvent('serviceworker', { timeout: 15000 }))
  const extId = new URL(sw.url()).host
  console.log('extension loaded:', extId)

  const page = await ctx.newPage()
  page.on('console', (m) => { if (m.type() === 'error') console.log('[page error]', m.text().slice(0, 200)) })
  await page.goto(pageUrl)
  await page.waitForTimeout(1500) // content script runs at document_idle

  // An extension page plays the side panel: it builds the same FILL_PAGE message and listens for status.
  const panel = await ctx.newPage()
  await panel.goto(`chrome-extension://${extId}/src/sidepanel/index.html`)
  const started = Date.now()
  const ack = await panel.evaluate(async ({ API, jobId, pageUrl, mode, resumeOverride }) => {
    window.__log = []
    chrome.runtime.onMessage.addListener((m) => {
      if (m && /^AGENT_/.test(m.type)) window.__log.push({ t: Date.now(), ...m })
      if (m && m.type === 'BACKEND_JSON' && m.path === '/api/v1/fill/plan') window.__planBody = m.init?.body
    })
    const token = (await (await fetch(`${API}/bootstrap`)).json()).token
    const fc = await (await fetch(`${API}/api/v1/fill-context?job_id=${encodeURIComponent(jobId)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })).json()
    const profile = {
      user_id: '', identity: { ...fc.identity, links: fc.identity?.links ?? [], locations: fc.identity?.locations ?? [] },
      roles: [], skills: (fc.skills ?? []).map((name) => ({ name, tags: [] })),
      projects: (fc.projects ?? []).map((p) => ({ title: p.title, bullets: p.bullets ?? [], skills: p.skills ?? [], role_tags: [] })),
      experience: fc.experience ?? [], education: [], publications: [], certifications: [],
      preferences: { target_roles: [], deal_breakers: [] }, anything_else: fc.anything_else ?? '', profile_version: 1,
    }
    const [tab] = await chrome.tabs.query({ url: pageUrl.split('#')[0] })
    window.__resume = fc.resume_url || null
    return chrome.runtime.sendMessage({ type: 'FILL_PAGE', tabId: tab.id, profile, resumeUrl: resumeOverride || fc.resume_url || undefined, jobId, mode })
  }, { API, jobId, pageUrl, mode, resumeOverride: process.env.RESUME_URL || '' })
  console.log('ack:', JSON.stringify(ack))

  let log = []
  for (let i = 0; i < 300; i++) { // up to 10 minutes
    log = await panel.evaluate(() => window.__log)
    if (log.some((m) => m.type === 'AGENT_DONE' || m.type === 'AGENT_ERROR')) break
    await panel.waitForTimeout(2000)
  }
  const seconds = (Date.now() - started) / 1000
  for (const m of log) console.log(`  +${((m.t - started) / 1000).toFixed(1)}s ${m.type === 'AGENT_STATUS' ? '' : m.type + ': '}${m.text ?? m.summary ?? m.message}`)

  // Blur everything first: the page's controlled input re-renders from its own state on blur.
  await page.evaluate(() => document.activeElement?.blur())
  await page.locator('body').click({ position: { x: 5, y: 5 } })
  const readFrame = () => {
    const out = {}
    for (const el of document.querySelectorAll('input, select, textarea')) {
      const key = el.name || el.id
      if (el.type === 'radio') { if (el.checked) out[key] = el.parentElement.textContent.trim(); else out[key] ??= ''; continue }
      if (el.type === 'checkbox') { out[key] = el.checked; continue }
      if (el.type === 'file') { out[key] = [...el.files].map((f) => `${f.name} (${f.size} B)`).join(', '); continue }
      out[key] = el.tagName === 'SELECT' ? (el.value ? el.selectedOptions[0].textContent : '') : el.value
    }
    if (window.__controlledState) out.__controlledState = window.__controlledState()
    out.__submitted = document.getElementById('submitted')?.textContent ?? ''
    for (const b of document.querySelectorAll('button[aria-pressed=true]')) out['__pressed:' + b.textContent.trim()] = true
    return out
  }
  const values = {}
  for (const [n, frame] of page.frames().entries()) {
    const v = await frame.evaluate(readFrame).catch(() => ({}))
    for (const [k, x] of Object.entries(v)) values[n ? `frame${n}:${k}` : k] = x
  }
  await page.screenshot({ path: out.replace(/\.json$/, '.png'), fullPage: true })
  const planBody = await panel.evaluate(() => window.__planBody || null)
  fs.writeFileSync(out, JSON.stringify({ ack, seconds, log, values, planBody }, null, 2))
  console.log(JSON.stringify(values, null, 2))
  console.log(`done in ${seconds.toFixed(1)}s`)
} finally {
  await ctx.close()
  fs.rmSync(profileDir, { recursive: true, force: true })
}
