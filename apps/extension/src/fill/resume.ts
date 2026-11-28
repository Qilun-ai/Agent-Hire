// SPDX-License-Identifier: AGPL-3.0-only
// Attaching the tailored résumé after a fill (assisted; custom upload widgets fall back to manual).

import { attachFile, blobToFile, findResumeInput } from './fileupload'
import { pageBackendBlob } from '../shared/backend'

/** "Ada_Lovelace_Resume.pdf" — recruiters see the file name; "resume.pdf" says nothing. */
export function resumeFileName(name: string | null | undefined): string {
  const safe = (name ?? '').normalize('NFKD').replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '_')
  return safe ? `${safe}_Resume.pdf` : 'Resume.pdf'
}

export async function tryAttachResume(path: string, filename: string, status: (text: string) => void) {
  const input = findResumeInput()
  if (!input) {
    status('No resume file input found — upload it manually.')
    return
  }
  try {
    const blob = await pageBackendBlob(path)
    if (!blob) {
      status('Could not download the resume — upload it manually.')
      return
    }
    const file = blobToFile(blob, filename)
    if (attachFile(input, file)) status('✓ Attached your tailored resume.')
    else status('This upload widget blocks auto-attach — drag the downloaded resume in manually.')
  } catch {
    status('Resume attach failed — upload it manually.')
  }
}

