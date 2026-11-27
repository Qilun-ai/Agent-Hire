import { describe, expect, it } from 'vitest'

import { AGENT_SYSTEM, fillTask, profileProse } from './agentTask'
import type { Profile } from './types'

const profile: Profile = {
  user_id: '',
  identity: {
    name: 'Asha Verma',
    email: 'asha@example.com',
    phone: '+91 90000 00000',
    links: ['https://github.com/asha'],
    citizenship: 'India',
    work_authorization: 'Authorized to work in India',
    locations: ['Bengaluru, India'],
    willing_to_relocate: true,
  },
  roles: ['Backend Engineer'],
  skills: [
    { name: 'Python', tags: [] },
    { name: 'FastAPI', tags: [] },
  ],
  projects: [{ title: 'Vaani', bullets: ['Built a real-time voice assistant.'], skills: ['Python'], role_tags: [] }],
  experience: [{ title: 'Senior Engineer', company: 'FlowCorp', dates: '2023 - Present', bullets: ['Built Vaani.'] }],
  education: ['BE Computer Science - Atria Institute of Technology, 2026'],
  publications: [],
  certifications: [],
  preferences: { target_roles: [], deal_breakers: ['no on-call'] },
  anything_else: 'Available immediately.',
  profile_version: 1,
}

describe('fillTask', () => {
  it('states the never-submit and no-navigation rules', () => {
    const task = fillTask(profile)
    expect(task).toMatch(/Do not click any button labelled Submit/i)
    expect(task).toMatch(/Skip file-upload fields/i)
    expect(task).toMatch(/ONLY the candidate profile/i)
  })

  it('embeds the stored profile facts verbatim', () => {
    const task = fillTask(profile)
    expect(task).toContain('Name: Asha Verma')
    expect(task).toContain('asha@example.com')
    expect(task).toContain('Bengaluru, India')
    expect(task).toContain('Skills: Python, FastAPI')
    expect(task).toContain('Target roles: Backend Engineer')
    expect(task).toContain('Vaani')
    expect(task).toContain('Built a real-time voice assistant.')
    expect(task).toContain('Senior Engineer')
    expect(task).toContain('FlowCorp')
    expect(task).toContain('Available immediately.')
  })

  it('never emits placeholder values for absent fields', () => {
    const prose = profileProse(profile)
    expect(prose).not.toMatch(/undefined|null/)
  })

  it('omits empty sections rather than printing empty labels', () => {
    const empty: Profile = {
      ...profile,
      roles: [],
      skills: [],
      projects: [],
      experience: [],
      education: [],
      certifications: [],
      publications: [],
      anything_else: '',
    }
    const prose = profileProse(empty)
    expect(prose).toContain('Name: Asha Verma')
    expect(prose).not.toMatch(/Skills:/)
    expect(prose).not.toMatch(/Projects:/)
    expect(prose).not.toMatch(/Anything else:/)
  })
})

describe('AGENT_SYSTEM', () => {
  it('keeps the safety contract in the system instruction too', () => {
    expect(AGENT_SYSTEM).toMatch(/never invent/i)
    expect(AGENT_SYSTEM).toMatch(/Submit, Apply, Next/)
    expect(AGENT_SYSTEM).toMatch(/file-upload/i)
  })
})
