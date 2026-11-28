import { describe, expect, it } from 'vitest'

import type { ExtensionJob } from '../shared/types'
import { hostOf, isFillableUrl, jobForTab } from './App'

const job = (job_id: string, url: string): ExtensionJob => ({
  job_id,
  url,
  title: job_id,
  company: '',
  location: '',
  status: 'discovered',
  resume_ready: false,
  prepared: false,
  prepared_at: '',
  in_cart: true,
})

describe('jobForTab', () => {
  it('matches the only job on the tab host, ignoring www', () => {
    const jobs = [job('a', 'https://www.acme.com/careers/1'), job('b', 'https://beta.io/jobs/2')]
    expect(jobForTab(jobs, 'https://acme.com/apply?step=2')?.job_id).toBe('a')
  })

  it('on a shared board host, needs the exact posting URL', () => {
    const jobs = [
      job('a', 'https://boards.greenhouse.io/acme/jobs/1'),
      job('b', 'https://boards.greenhouse.io/beta/jobs/2'),
    ]
    expect(jobForTab(jobs, 'https://boards.greenhouse.io/beta/jobs/2#app')?.job_id).toBe('b')
    expect(jobForTab(jobs, 'https://boards.greenhouse.io/other/jobs/9')).toBeNull()
  })

  it('never matches a non-web tab', () => {
    expect(jobForTab([job('a', 'https://acme.com')], 'chrome://extensions')).toBeNull()
  })
})

describe('helpers', () => {
  it('hostOf survives junk', () => {
    expect(hostOf('not a url')).toBe('')
    expect(hostOf(undefined)).toBe('')
  })

  it('only http(s) pages are fillable', () => {
    expect(isFillableUrl('https://acme.com/apply')).toBe(true)
    expect(isFillableUrl('chrome://newtab')).toBe(false)
    expect(isFillableUrl('https://chromewebstore.google.com/detail/x')).toBe(false)
  })
})

import { relayDetail } from '../llm-client'

describe('relayDetail', () => {
  it('pulls the reason out of FastAPI and OpenAI error bodies', () => {
    expect(relayDetail('{"detail":"provider \'ollama\' rejected the agent request"}')).toContain('ollama')
    expect(relayDetail('{"error":{"message":"LLM relay failed"}}')).toBe('LLM relay failed')
    expect(relayDetail('')).toBe('no response')
  })
})

import { resumeFileName } from '../fill/resume'

describe('resumeFileName', () => {
  it('names the file after the candidate', () => {
    expect(resumeFileName('Ada Lovelace')).toBe('Ada_Lovelace_Resume.pdf')
    expect(resumeFileName('José  O\'Neil')).toBe('Jose_ONeil_Resume.pdf')
    expect(resumeFileName('')).toBe('Resume.pdf')
  })
})
