// Side panel (docs/11 §W3). The website is where jobs are found and résumés tailored; this panel
// works through the apply cart: open a job's page, fill the application form on it, and mark the
// job applied once the user has submitted. It never submits, and since the backend makes every
// value decision, there is nothing to configure here.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BackendError,
  appUrl,
  checkAgentToolCalling,
  getFillContextForJob,
  health,
  listJobs,
  markApplied,
} from '../shared/api'
import { fillContextToProfile } from '../shared/fillProfile'
import type { ExtensionMessage, FillPage } from '../shared/messages'
import type { ExtensionJob } from '../shared/types'

type Conn = 'checking' | 'ok' | 'down'
type AgentCheck = 'unknown' | 'checking' | 'ok' | 'fail'
type LogLine = { text: string; tone: 'info' | 'ok' | 'warn' | 'bad' }
type ActiveTab = { id?: number; url: string; title: string }

const SELECTED_KEY = 'agent_hire.selectedJob'

export function hostOf(url: string | undefined): string {
  try {
    return new URL(url ?? '').hostname.replace(/^www\./, '').toLowerCase()
  } catch {
    return ''
  }
}

/** The job whose page is open in the active tab, by host. A job board host (LinkedIn, Indeed) can
 *  hold many jobs, so this is only a hint — the user's own pick always wins. */
export function jobForTab(jobs: ExtensionJob[], tabUrl: string): ExtensionJob | null {
  const host = hostOf(tabUrl)
  if (!host) return null
  const matches = jobs.filter((job) => hostOf(job.url) === host)
  if (matches.length === 1) return matches[0]
  return matches.find((job) => tabUrl.startsWith(job.url)) ?? null
}

export function isFillableUrl(url: string): boolean {
  return /^https?:/i.test(url) && !url.startsWith('https://chromewebstore.google.com')
}

export default function App() {
  const [conn, setConn] = useState<Conn>('checking')
  const [agentCheck, setAgentCheck] = useState<AgentCheck>('unknown')
  const [agentDetail, setAgentDetail] = useState('')
  const [jobs, setJobs] = useState<ExtensionJob[]>([])
  const [jobsError, setJobsError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tab, setTab] = useState<ActiveTab>({ url: '', title: '' })
  const [filling, setFilling] = useState(false)
  const [confirmApplied, setConfirmApplied] = useState(false)
  const [busyApplied, setBusyApplied] = useState(false)
  const [log, setLog] = useState<LogLine[]>([])
  const logRef = useRef<HTMLDivElement>(null)

  const push = useCallback((text: string, tone: LogLine['tone'] = 'info') => {
    setLog((lines) => [...lines.slice(-200), { text, tone }])
  }, [])

  const refresh = useCallback(async () => {
    setConn('checking')
    try {
      await health()
      setConn('ok')
    } catch {
      setConn('down')
      return
    }
    try {
      setJobs(await listJobs())
      setJobsError('')
    } catch (e) {
      setJobsError(e instanceof BackendError ? `Could not load your jobs (${e.status}).` : 'Could not load your jobs.')
    }
  }, [])

  const readTab = useCallback(async () => {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true })
    setTab({ id: active?.id, url: active?.url ?? '', title: active?.title ?? '' })
  }, [])

  // Start-up: connection, jobs, the active tab, and the job the user last picked.
  useEffect(() => {
    void refresh()
    void readTab()
    void chrome.storage?.local
      ?.get(SELECTED_KEY)
      .then((stored) => {
        const id = stored?.[SELECTED_KEY]
        if (typeof id === 'string') setSelectedId((current) => current ?? id)
      })
      .catch(() => undefined)
  }, [refresh, readTab])

  // Follow the active tab: switching tabs or navigating changes what "this page" means.
  useEffect(() => {
    const onActivated = () => void readTab()
    const onUpdated = (_id: number, change: { url?: string; status?: string }, updated: chrome.tabs.Tab) => {
      if (updated.active && (change.url || change.status === 'complete')) void readTab()
    }
    chrome.tabs.onActivated.addListener(onActivated)
    chrome.tabs.onUpdated.addListener(onUpdated)
    const onFocus = () => void refresh()
    window.addEventListener('focus', onFocus)
    return () => {
      chrome.tabs.onActivated.removeListener(onActivated)
      chrome.tabs.onUpdated.removeListener(onUpdated)
      window.removeEventListener('focus', onFocus)
    }
  }, [readTab, refresh])

  // Live fill status from the content script.
  useEffect(() => {
    const listener = (msg: ExtensionMessage) => {
      if (msg.type === 'AGENT_STATUS') push(msg.text)
      else if (msg.type === 'AGENT_DONE') {
        push(msg.summary, msg.success ? 'ok' : 'warn')
        setFilling(false)
      } else if (msg.type === 'AGENT_ERROR') {
        push(msg.message, 'bad')
        setFilling(false)
      }
    }
    chrome.runtime.onMessage.addListener(listener)
    return () => chrome.runtime.onMessage.removeListener(listener)
  }, [push])

  useEffect(() => {
    logRef.current?.scrollTo(0, logRef.current.scrollHeight)
  }, [log])

  const tabJob = useMemo(() => jobForTab(jobs, tab.url), [jobs, tab.url])
  const selected = useMemo(
    () => jobs.find((job) => job.job_id === selectedId) ?? tabJob ?? jobs[0] ?? null,
    [jobs, selectedId, tabJob],
  )

  const choose = (jobId: string) => {
    setSelectedId(jobId)
    setConfirmApplied(false)
    void chrome.storage?.local?.set({ [SELECTED_KEY]: jobId }).catch(() => undefined)
  }

  function openJob(job: ExtensionJob) {
    choose(job.job_id)
    if (job.url) void chrome.tabs.create({ url: job.url })
  }

  /** "decision": saved values chosen by the decision model (default). "agent": the LLM page agent fallback. */
  function fillThisPage(mode: 'decision' | 'agent' = 'decision') {
    if (!selected || filling) return
    const tabUrl = tab.url
    if (!isFillableUrl(tabUrl)) {
      push('Open the job application in a normal web page first.', 'warn')
      return
    }
    // The permission prompt must be the first thing this click does: Chrome only shows it during
    // a user gesture, and any await before it can lose the gesture. Already-granted sites resolve
    // immediately without a prompt.
    const permission = chrome.permissions.request({ origins: [new URL(tabUrl).origin + '/*'] })
    setFilling(true)
    setLog([{ text: mode === 'agent' ? `AI agent filling for ${selected.title || 'this job'}…` : `Filling for ${selected.title || 'this job'}…`, tone: 'info' }])
    void (async () => {
      try {
        if (!(await permission.catch(() => false))) {
          push('Permission for this site was denied — the form cannot be filled.', 'bad')
          setFilling(false)
          return
        }
        const ctx = await getFillContextForJob(selected.job_id)
        if (!ctx.resume_url) push('No tailored résumé yet — filling from your profile only.', 'warn')
        const msg: FillPage = {
          type: 'FILL_PAGE',
          tabId: tab.id,
          profile: fillContextToProfile(ctx),
          resumeUrl: ctx.resume_url || undefined,
          jobId: ctx.job_id || selected.job_id,
          mode,
        }
        const ack = (await chrome.runtime.sendMessage(msg)) as { ack: boolean; error?: string } | undefined
        if (!ack?.ack) {
          push(ack?.error ?? 'Could not start the fill.', 'bad')
          setFilling(false)
        }
      } catch (e) {
        push(e instanceof Error ? e.message : 'Fill failed to start.', 'bad')
        setFilling(false)
      }
    })()
  }

  function stopFill() {
    void chrome.runtime.sendMessage({ type: 'STOP_FILL', tabId: tab.id })
    push('Stopping…')
  }

  async function confirmAppliedNow() {
    if (!selected) return
    setBusyApplied(true)
    try {
      await markApplied(selected.job_id)
      push(`Marked "${selected.title}" as applied. It now counts in your Analytics.`, 'ok')
      setConfirmApplied(false)
      setSelectedId(null)
      await refresh()
    } catch (e) {
      push(e instanceof Error ? `Could not mark it applied: ${e.message}` : 'Could not mark it applied.', 'bad')
    } finally {
      setBusyApplied(false)
    }
  }

  async function checkAgent() {
    setAgentCheck('checking')
    setAgentDetail('')
    const result = await checkAgentToolCalling()
    setAgentCheck(result.ok ? 'ok' : 'fail')
    setAgentDetail(result.detail)
  }

  const openApp = () => void chrome.tabs.create({ url: appUrl() })
  const onJobPage = Boolean(selected && tabJob && tabJob.job_id === selected.job_id)

  return (
    <div className="panel">
      <header className="panel-header">
        <div className="panel-brand">
          <img className="panel-logo" src="/agent-hire-logo.png" alt="" width={30} height={30} />
          <div>
            <div className="panel-brand-name">Agent-Hire</div>
            <div className="panel-brand-sub">Apply assistant</div>
          </div>
        </div>
        <div className="panel-checks">
          <button
            type="button"
            onClick={() => void checkAgent()}
            disabled={agentCheck === 'checking' || conn !== 'ok'}
            title="Check that the configured model can drive the form filler"
            className="chip"
          >
            <span className={`chip-dot ${agentCheck === 'ok' ? 'ok' : agentCheck === 'fail' ? 'fail' : agentCheck === 'checking' ? 'checking' : ''}`} />
            {agentCheck === 'ok' ? 'AI ready' : agentCheck === 'checking' ? 'Checking' : agentCheck === 'fail' ? 'AI not ready' : 'Check AI'}
          </button>
          <button type="button" onClick={() => void refresh()} title="Reconnect and reload jobs" className="chip">
            <span className={`chip-dot ${conn === 'ok' ? 'ok' : conn === 'checking' ? 'checking' : 'fail'}`} />
            {conn === 'ok' ? 'Online' : conn === 'checking' ? '…' : 'Offline'}
          </button>
        </div>
      </header>

      <main className="panel-main">
        {agentCheck === 'fail' && agentDetail && <p className="notice notice-bad">Form filler unavailable: {agentDetail}</p>}

        {conn === 'down' ? (
          <section className="empty">
            <span className="kicker">Not connected</span>
            <h2>Agent-Hire isn't running</h2>
            <p>Start it (<code>agent-hire open</code>, or Agent-Hire in your app menu), then reconnect.</p>
            <button type="button" className="btn btn-primary" onClick={() => void refresh()}>
              Reconnect
            </button>
          </section>
        ) : (
          <>
            <section className="tab-strip" aria-label="Current tab">
              <span className="kicker">This tab</span>
              <div className="tab-strip-row">
                <span className="tab-host" title={tab.url}>{isFillableUrl(tab.url) ? hostOf(tab.url) : 'No web page open'}</span>
                {tabJob && <span className="badge badge-ok">Job page</span>}
              </div>
            </section>

            {selected && (
              <section className="current card" aria-label="Selected job">
                <span className="kicker">Applying to</span>
                <h2>{selected.title || 'Untitled job'}</h2>
                <div className="job-company">{[selected.company, selected.location].filter(Boolean).join(' · ')}</div>
                <div className="badges">
                  <span className={`badge ${selected.resume_ready ? 'badge-ok' : 'badge-warn'}`}>
                    {selected.resume_ready ? 'Résumé ready' : 'No tailored résumé'}
                  </span>
                  {selected.in_cart && <span className="badge">In cart</span>}
                </div>

                <ol className="steps">
                  <li className={onJobPage ? 'done' : ''}>
                    <span>Open the job page and sign in.</span>
                    <button type="button" className="btn btn-sm" disabled={!selected.url} onClick={() => openJob(selected)}>
                      Open job
                    </button>
                  </li>
                  <li>
                    <span>Click the site's Apply button, then fill the form.</span>
                    {filling ? (
                      <button type="button" className="btn btn-sm btn-danger" onClick={stopFill}>
                        Stop
                      </button>
                    ) : (
                      <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <button type="button" className="btn btn-sm btn-fill" onClick={() => fillThisPage('decision')} disabled={!isFillableUrl(tab.url)}>
                          Fill this page
                        </button>
                        <button type="button" className="btn btn-sm" onClick={() => fillThisPage('agent')} disabled={!isFillableUrl(tab.url)}
                          title="For unusual forms: an AI agent reads the page and fills it step by step (slower, uses your AI model)">
                          Fill with AI agent
                        </button>
                      </span>
                    )}
                  </li>
                  <li>
                    <span>Review, submit it yourself, then mark it done.</span>
                    {confirmApplied ? (
                      <span className="confirm-row">
                        <button type="button" className="btn btn-sm btn-ok" disabled={busyApplied} onClick={() => void confirmAppliedNow()}>
                          {busyApplied ? 'Saving…' : 'Yes, I submitted'}
                        </button>
                        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setConfirmApplied(false)}>
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button type="button" className="btn btn-sm" onClick={() => setConfirmApplied(true)}>
                        Mark applied
                      </button>
                    )}
                  </li>
                </ol>
                <p className="hint">The filler never presses Submit. Multi-page form? Go to the next page and fill again.</p>
              </section>
            )}

            {log.length > 0 && (
              <div ref={logRef} className="fill-log" aria-live="polite">
                {log.map((line, i) => (
                  <div key={i} className={`log-${line.tone}`}>
                    {line.text}
                  </div>
                ))}
              </div>
            )}

            <section aria-label="Your jobs">
              <div className="section-head">
                <span className="kicker">Your jobs · {jobs.length}</span>
                <button type="button" className="btn-link" onClick={() => void refresh()}>
                  Refresh
                </button>
              </div>
              {jobsError && <p className="notice notice-bad">{jobsError}</p>}
              {jobs.length === 0 && !jobsError ? (
                <div className="empty empty-sm">
                  <p>
                    No jobs queued yet. In Agent-Hire, tick jobs in <strong>Jobs</strong> and choose <strong>Add to applications</strong>,
                    or ask the agent to shortlist some, and they show up here.
                  </p>
                </div>
              ) : (
                <ul className="job-list">
                  {jobs.map((job) => {
                    const active = selected?.job_id === job.job_id
                    return (
                      <li key={job.job_id}>
                        <button
                          type="button"
                          className={`job-row${active ? ' active' : ''}`}
                          aria-pressed={active}
                          onClick={() => choose(job.job_id)}
                        >
                          <span className="job-mark">{(job.company || job.title || '?').trim().charAt(0).toUpperCase()}</span>
                          <span className="job-row-copy">
                            <strong>{job.title || 'Untitled job'}</strong>
                            <small>{job.company || hostOf(job.url)}</small>
                          </span>
                          {tabJob?.job_id === job.job_id && <span className="badge badge-ok">Open</span>}
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>
          </>
        )}
      </main>

      <footer className="panel-footer">
        <button type="button" className="btn-link" onClick={openApp}>
          Open Agent-Hire app ↗
        </button>
      </footer>
    </div>
  )
}
