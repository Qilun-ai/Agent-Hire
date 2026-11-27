import { defineManifest } from '@crxjs/vite-plugin'

export default defineManifest({
  manifest_version: 3,
  name: 'Agent-Hire',
  version: '0.2.0',
  description:
    'Work through your Agent-Hire apply cart: open each job, fill its application form with your profile, and mark it applied.',

  // The backend is a fixed local sidecar (src/shared/backend.ts), so its origin is a static
  // host permission — no settings screen, no runtime grant. activeTab/scripting/tabs drive the
  // autofiller; sidePanel is the workbench; storage remembers the job picked in the panel.
  permissions: ['sidePanel', 'activeTab', 'scripting', 'tabs', 'storage'],
  // All sites, declared once. The content script below already runs on every site (that is the
  // install-time "read and change all your data" grant), but Chrome only lets chrome.scripting —
  // the richest-frame scan, cross-origin iframes (Greenhouse/Lever embeds) and re-injecting into
  // tabs opened before an update — touch hosts listed here. With only optional per-site grants,
  // every new job site needed a permission prompt and iframe forms on another origin never filled.
  // This also covers the local API on :8000/:8080.
  host_permissions: ['<all_urls>'],

  background: {
    service_worker: 'src/background.ts',
    type: 'module',
  },

  side_panel: {
    default_path: 'src/sidepanel/index.html',
  },

  action: {
    default_title: 'Open Agent-Hire',
    default_icon: 'agent-hire-logo.png',
  },

  icons: {
    16: 'agent-hire-logo.png',
    32: 'agent-hire-logo.png',
    48: 'agent-hire-logo.png',
    128: 'agent-hire-logo.png',
  },

  // The autofiller's listener. Declared for all frames (Greenhouse/Lever forms live in iframes);
  // the background worker picks the single richest frame to actually fill (docs/06 §3). The
  // listener is tiny — the page-agent bundle is imported only when a fill starts.
  content_scripts: [
    {
      matches: ['<all_urls>'],
      js: ['src/content.ts'],
      all_frames: true,
      run_at: 'document_idle',
    },
  ],
})
