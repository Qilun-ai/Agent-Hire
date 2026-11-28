// SPDX-License-Identifier: AGPL-3.0-only
// Find the fillable fields on a page and describe each one the way a person reads it: the question
// (label), its type, the options it offers and a little nearby text. The backend decides what goes
// where from these descriptions; nothing here guesses values.

export interface ScannedField {
  id: string
  kind: string // text | textarea | email | tel | url | number | date | select | radio | checkbox | checkboxes | combobox
  label: string
  name: string
  placeholder: string
  aria: string
  autocomplete: string
  options: string[]
  required: boolean
  context: string
}

/** A group of checkboxes that answer one question ("Which languages…?"): several may be ticked. */
export interface CheckboxGroup { widget: 'checkboxes'; boxes: HTMLInputElement[] }
/** Answer buttons that act as a radio group (Ashby-style Yes / No). */
export interface ButtonGroup { widget: 'buttons'; buttons: HTMLButtonElement[] }
/** A searchable dropdown (React-Select, Downshift, ARIA combobox): only clicking an option counts. */
export interface Combobox { widget: 'combobox'; input: HTMLInputElement }

export type FieldTarget =
  | HTMLInputElement
  | HTMLSelectElement
  | HTMLTextAreaElement
  | HTMLInputElement[]
  | CheckboxGroup
  | ButtonGroup
  | Combobox

const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password', 'search', 'range', 'color'])
const MAX_FIELDS = 250

const clean = (text: string | null | undefined, max = 300): string =>
  (text ?? '').replace(/\s+/g, ' ').replace(/[*✱]\s*$/, '').trim().slice(0, max)

export function isVisible(el: Element): boolean {
  const rect = el.getBoundingClientRect()
  if (rect.width === 0 && rect.height === 0) return false
  const style = getComputedStyle(el)
  return style.visibility !== 'hidden' && style.display !== 'none'
}

function textOfIds(ids: string | null): string {
  if (!ids) return ''
  return ids
    .split(/\s+/)
    .map((id) => document.getElementById(id)?.textContent ?? '')
    .join(' ')
}

/** The question a field asks, as a person would read it. */
export function labelFor(el: HTMLElement): string {
  const labelled = textOfIds(el.getAttribute('aria-labelledby'))
  if (clean(labelled)) return clean(labelled)
  const own = (el as HTMLInputElement).labels
  if (own && own.length) {
    const text = Array.from(own).map((l) => l.textContent ?? '').join(' ')
    if (clean(text)) return clean(text)
  }
  const aria = el.getAttribute('aria-label')
  if (clean(aria)) return clean(aria)
  const wrapping = el.closest('label')
  if (wrapping && clean(wrapping.textContent)) return clean(wrapping.textContent)
  return questionNear(el)
}

/** Text of the nearest container that looks like the field's question block. */
function questionNear(el: Element): string {
  let node: Element | null = el.parentElement
  for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
    const legend = node.querySelector(':scope > legend')
    if (legend && clean(legend.textContent)) return clean(legend.textContent)
    const text = clean(directText(node))
    if (text) return text
  }
  return ''
}

/** Text that belongs to this container itself, not to its form controls or option lists. */
function directText(node: Element): string {
  const clone = node.cloneNode(true) as Element
  clone
    .querySelectorAll('input, select, textarea, option, button, script, style, [role="listbox"], [role="radiogroup"], label:has(input)')
    .forEach((n) => n.remove())
  const text = clean(clone.textContent, 400)
  return text.length > 200 ? '' : text // a whole form section is not a label
}

function contextFor(el: Element, label: string): string {
  const block = el.closest('fieldset, [role="group"], [role="radiogroup"], .form-group, .field, li, div')
  const text = block ? clean(directText(block), 400) : ''
  return text && text !== label ? text.slice(0, 300) : ''
}

export function isCombobox(el: Element): boolean {
  if (!(el instanceof HTMLInputElement)) return false
  const auto = (el.getAttribute('aria-autocomplete') || '').toLowerCase()
  return el.getAttribute('role') === 'combobox' || auto === 'list' || auto === 'both' || el.getAttribute('aria-haspopup') === 'listbox'
}

/** The option list a combobox controls, if it is open (or always rendered). */
export function comboOptions(input: HTMLInputElement): HTMLElement[] {
  const ids = `${input.getAttribute('aria-controls') ?? ''} ${input.getAttribute('aria-owns') ?? ''}`.trim()
  const lists = ids ? ids.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean) as HTMLElement[] : []
  const scope: ParentNode[] = lists.length ? lists : [document]
  return scope.flatMap((root) => Array.from(root.querySelectorAll<HTMLElement>('[role="option"]'))).filter(isVisible)
}

function kindOf(el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): string {
  if (isCombobox(el)) return 'combobox'
  if (el instanceof HTMLSelectElement) return 'select'
  if (el instanceof HTMLTextAreaElement) return 'textarea'
  const t = (el.type || 'text').toLowerCase()
  return ['email', 'tel', 'url', 'number', 'date', 'radio', 'checkbox'].includes(t) ? t : 'text'
}

/** A radio option's own text (never the question: that would make every option look the same). */
function optionLabel(radio: HTMLInputElement): string {
  const own = radio.labels && radio.labels.length ? Array.from(radio.labels).map((l) => l.textContent ?? '').join(' ') : ''
  return clean(own || textOfIds(radio.getAttribute('aria-labelledby')) || radio.getAttribute('aria-label') || radio.value, 120)
}

const NOT_AN_ANSWER = /^(next|back|previous|continue|submit|apply|cancel|save|upload|attach|add|remove|delete|edit|clear|close|browse|search|show|hide|more|less|×|x|\+|-)\b/i

/** Buttons that are the answers to one question: 2–6 short type=button siblings and nothing else in their container. */
function buttonGroups(root: ParentNode): HTMLButtonElement[][] {
  const byParent = new Map<Element, HTMLButtonElement[]>()
  for (const b of root.querySelectorAll<HTMLButtonElement>('button')) {
    if (b.type !== 'button' || b.disabled || !b.parentElement || !isVisible(b)) continue
    const list = byParent.get(b.parentElement) ?? []
    list.push(b)
    byParent.set(b.parentElement, list)
  }
  const groups: HTMLButtonElement[][] = []
  for (const [parent, buttons] of byParent) {
    if (buttons.length < 2 || buttons.length > 6) continue
    const others = Array.from(parent.children).filter((c) => !(c instanceof HTMLButtonElement) && !(c instanceof HTMLInputElement && c.type === 'hidden'))
    if (others.length) continue
    const texts = buttons.map((b) => clean(b.textContent, 60))
    if (texts.some((t) => !t || t.length > 40 || NOT_AN_ANSWER.test(t))) continue
    if (!parent.closest('form, [role="form"], [role="group"], [role="radiogroup"], fieldset') && !buttons.some((b) => b.hasAttribute('aria-pressed') || b.getAttribute('role') === 'radio')) continue
    groups.push(buttons)
  }
  return groups
}

/**
 * Read the options of searchable dropdowns that only render them while open: open each one briefly,
 * read the list, close it. Without options the backend can only guess the wording ("India" vs "IN").
 */
export async function harvestComboOptions(fields: ScannedField[], targets: Map<string, FieldTarget>, limit = 15): Promise<void> {
  let opened = 0
  for (const field of fields) {
    const target = targets.get(field.id)
    if (field.kind !== 'combobox' || field.options.length || !target || !('widget' in target) || target.widget !== 'combobox') continue
    if (++opened > limit) break
    const input = target.input
    input.focus()
    input.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    input.click()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await new Promise((r) => setTimeout(r, 200))
    field.options = comboOptions(input).map((o) => clean(o.textContent, 120)).filter(Boolean).slice(0, 80)
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    input.blur()
  }
}

/** Fillable fields on the page. Each element gets a data-gh-fid attribute for the apply step. */
export function scanFields(root: ParentNode = document): { fields: ScannedField[]; targets: Map<string, FieldTarget> } {
  const fields: ScannedField[] = []
  const targets = new Map<string, FieldTarget>()
  const radioGroups = new Map<string, HTMLInputElement[]>()
  let n = 0
  const nextId = () => `f${++n}`

  for (const el of root.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('input, select, textarea')) {
    if (fields.length >= MAX_FIELDS) break
    if (el.disabled || (el as HTMLInputElement).readOnly || !isVisible(el)) continue
    if (el instanceof HTMLInputElement && SKIP_TYPES.has(el.type.toLowerCase())) continue
    const kind = kindOf(el)
    if (kind === 'radio') {
      const key = (el as HTMLInputElement).name || el.closest('[role="radiogroup"], fieldset')?.id || `radio-${n}`
      const group = radioGroups.get(key) ?? []
      group.push(el as HTMLInputElement)
      radioGroups.set(key, group)
      continue
    }
    if (kind === 'checkbox' && (el as HTMLInputElement).name) {
      const name = (el as HTMLInputElement).name
      const same = Array.from(root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')).filter((c) => c.name === name && isVisible(c))
      if (same.length > 1) {
        if (same[0] !== el) continue // the group is added once, at its first box
        const id = nextId()
        same.forEach((c) => c.setAttribute('data-gh-fid', id))
        const group = el.closest('fieldset, [role="group"]')
        const question =
          clean(group?.querySelector('legend')?.textContent) ||
          clean(textOfIds(group?.getAttribute('aria-labelledby') ?? null)) ||
          clean(group?.getAttribute('aria-label')) ||
          questionNear(group ?? el.parentElement ?? el)
        fields.push({ id, kind: 'checkboxes', label: question, name: clean(name, 120), placeholder: '', aria: '', autocomplete: '',
          options: same.map(optionLabel), required: same.some((c) => c.required), context: '' })
        targets.set(id, { widget: 'checkboxes', boxes: same })
        continue
      }
    }
    const id = nextId()
    el.setAttribute('data-gh-fid', id)
    const label = labelFor(el)
    fields.push({
      id,
      kind,
      label,
      name: clean(el.name, 120),
      placeholder: clean((el as HTMLInputElement).placeholder, 200),
      aria: clean(el.getAttribute('aria-label'), 200),
      autocomplete: clean(el.getAttribute('autocomplete'), 60),
      options:
        el instanceof HTMLSelectElement
          ? Array.from(el.options).map((o) => clean(o.text, 120)).filter(Boolean)
          : kind === 'combobox'
            ? comboOptions(el as HTMLInputElement).map((o) => clean(o.textContent, 120)).filter(Boolean).slice(0, 80)
            : [],
      required: el.required || el.getAttribute('aria-required') === 'true',
      context: contextFor(el, label),
    })
    targets.set(id, kind === 'combobox' ? { widget: 'combobox', input: el as HTMLInputElement } : el)
  }

  for (const radios of radioGroups.values()) {
    if (fields.length >= MAX_FIELDS) break
    const id = nextId()
    radios.forEach((r) => r.setAttribute('data-gh-fid', id))
    const group = radios[0].closest('fieldset, [role="radiogroup"]')
    const question =
      clean(group?.querySelector('legend')?.textContent) ||
      clean(textOfIds(group?.getAttribute('aria-labelledby') ?? null)) ||
      clean(group?.getAttribute('aria-label')) ||
      (group ? clean(directText(group)) : '') ||
      questionNear(group ?? radios[0].parentElement ?? radios[0])
    fields.push({
      id,
      kind: 'radio',
      label: question,
      name: clean(radios[0].name, 120),
      placeholder: '',
      aria: '',
      autocomplete: '',
      options: radios.map(optionLabel),
      required: radios.some((r) => r.required),
      context: '',
    })
    targets.set(id, radios)
  }
  for (const buttons of buttonGroups(root)) {
    if (fields.length >= MAX_FIELDS) break
    const parent = buttons[0].parentElement!
    const id = nextId()
    buttons.forEach((b) => b.setAttribute('data-gh-fid', id))
    const question =
      clean(textOfIds(parent.getAttribute('aria-labelledby'))) || clean(parent.getAttribute('aria-label')) || questionNear(parent)
    fields.push({ id, kind: 'radio', label: question, name: '', placeholder: '', aria: '', autocomplete: '',
      options: buttons.map((b) => clean(b.textContent, 60)), required: false, context: '' })
    targets.set(id, { widget: 'buttons', buttons })
  }
  return { fields, targets }
}
