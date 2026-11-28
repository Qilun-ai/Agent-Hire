// Profile plumbing for the agent fill.

import type { FillContext, Profile } from './types'

/** Adapt a per-job fill context (docs/11) into the Profile shape the agent task consumes.
 *  Identity + anything-else are the fixed facts; skills/projects/experience are job-tailored. */
export function fillContextToProfile(ctx: FillContext): Profile {
  return {
    user_id: '',
    identity: {
      ...ctx.identity,
      links: ctx.identity.links ?? [],
      locations: ctx.identity.locations ?? [],
    },
    roles: [],
    skills: (ctx.skills ?? []).map((name) => ({ name, tags: [] })),
    projects: (ctx.projects ?? []).map((p) => ({
      title: p.title,
      bullets: p.bullets ?? [],
      skills: p.skills ?? [],
      role_tags: [],
    })),
    experience: ctx.experience ?? [],
    education: [],
    publications: [],
    certifications: [],
    preferences: { target_roles: [], deal_breakers: [] },
    anything_else: ctx.anything_else ?? '',
    profile_version: 1,
  }
}
