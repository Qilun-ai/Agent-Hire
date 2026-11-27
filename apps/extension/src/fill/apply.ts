// SPDX-License-Identifier: AGPL-3.0-only
// Put planned values into the page. Values are written through the native setters plus input/change
// events so framework-controlled inputs (React, Vue) register them, fields the user already filled
// are left alone, and every write is read back: a value the page rejected counts as not filled.

import { comboOptions, type FieldTarget } from './scan'

export interface Fill {
  id: string
  value: string
}

export interface ApplyResult {
  filled: string[]
  kept: string[] // the user had already filled it
  failed: { id: string; reason: string }[]
}

const norm = (s: string) => s.replace(/\s+/g, ' ').trim().toLowerCase()

function fire(el: HTMLElement) {
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
  el.dispatchEvent(new Event('blur', { bubbles: true }))
}

function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const proto =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
  if (setter) setter.call(el, value)
  else el.value = value
}

function radioText(r: HTMLInputElement): string {
  const own = r.labels && r.labels.length ? Array.from(r.labels).map((l) => l.textContent ?? '').join(' ') : ''
  return norm(own || r.getAttribute('aria-label') || r.value)
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function press(el: HTMLElement) {
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }))
  }
  el.click()
}

const isPressed = (b: HTMLElement) =>
  b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true' || b.getAttribute('aria-selected') === 'true' ||
  /\b(selected|active|checked|is-selected)\b/i.test(b.className)

/** The option that plainly matches: same text, else the only one that starts with / contains the value. */
function bestOption<T extends HTMLElement>(options: T[], value: string, text: (o: T) => string = (o) => o.textContent ?? ''): T | undefined {
  const v = norm(value)
  if (!v) return undefined
  const exact = options.find((o) => norm(text(o)) === v)
  if (exact) return exact
  const starts = options.filter((o) => norm(text(o)).startsWith(v))
  if (starts.length === 1) return starts[0]
  const contains = options.filter((o) => norm(text(o)).includes(v))
  return contains.length === 1 ? contains[0] : undefined
}

async function applyCombobox(input: HTMLInputElement, value: string): Promise<'filled' | 'kept' | string> {
  const box = input.closest('[class*="container"], [class*="select"], [class*="combo"]') ?? input.parentElement
  const shown = box?.querySelector('[class*="singleValue"], [class*="single-value"], [class*="selected-value"]')?.textContent?.trim()
  if (input.value.trim() || shown) return 'kept'
  input.focus()
  press(input)
  setNativeValue(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
  let option: HTMLElement | undefined
  for (let waited = 0; waited < 2000 && !option; waited += 100) {
    await sleep(100)
    option = bestOption(comboOptions(input), value)
  }
  if (!option) {
    setNativeValue(input, '') // never leave half-typed text behind
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.blur()
    return `no option matches "${value}"`
  }
  const chosen = norm(option.textContent ?? '')
  press(option)
  await sleep(120)
  input.blur()
  await sleep(60)
  const after = box?.querySelector('[class*="singleValue"], [class*="single-value"], [class*="selected-value"]')?.textContent ?? ''
  return norm(input.value) === chosen || norm(after) === chosen || norm(box?.textContent ?? '').includes(chosen)
    ? 'filled'
    : 'the page did not accept the choice'
}

async function applyOne(target: FieldTarget, value: string): Promise<'filled' | 'kept' | string> {
  if (!Array.isArray(target) && 'widget' in target) {
    if (target.widget === 'combobox') return applyCombobox(target.input, value)
    if (target.widget === 'buttons') {
      if (target.buttons.some(isPressed)) return 'kept'
      const button = bestOption(target.buttons, value)
      if (!button) return `no option "${value}"`
      press(button)
      await sleep(50)
      const tracked = target.buttons.some((b) => b.hasAttribute('aria-pressed') || b.hasAttribute('aria-checked'))
      return !tracked || isPressed(button) ? 'filled' : 'the page did not accept the choice'
    }
    // checkbox group: tick every option the answer names; never untick anything
    if (target.boxes.some((b) => b.checked)) return 'kept'
    const wanted = value.split(/\n|;|,/).map((v) => v.trim()).filter(Boolean)
    const boxes = wanted.map((w) => target.boxes.find((b) => radioText(b) === norm(w)) ?? target.boxes.find((b) => norm(b.value) === norm(w)))
    const hits = boxes.filter((b): b is HTMLInputElement => !!b)
    if (!hits.length) return `no option "${value}"`
    for (const b of hits) if (!b.checked) b.click()
    return hits.every((b) => b.checked) ? 'filled' : 'the page did not accept the tick'
  }
  if (Array.isArray(target)) {
    if (target.some((r) => r.checked)) return 'kept'
    const radio = target.find((r) => radioText(r) === norm(value)) ?? target.find((r) => norm(r.value) === norm(value))
    if (!radio) return `no option "${value}"`
    radio.click()
    if (!radio.checked) {
      radio.checked = true
      fire(radio)
    }
    return radio.checked ? 'filled' : 'the page did not accept the choice'
  }
  if (target instanceof HTMLSelectElement) {
    const current = target.selectedOptions[0]
    if (current && current.value && target.selectedIndex > 0) return 'kept'
    const option =
      Array.from(target.options).find((o) => norm(o.text) === norm(value)) ??
      Array.from(target.options).find((o) => norm(o.value) === norm(value))
    if (!option) return `no option "${value}"`
    setNativeValue(target, option.value)
    fire(target)
    return target.value === option.value ? 'filled' : 'the page did not accept the choice'
  }
  if (target instanceof HTMLInputElement && target.type.toLowerCase() === 'checkbox') {
    const want = /^(yes|true|y|1|checked|on)$/i.test(value.trim())
    if (target.checked === want) return 'kept'
    if (!want) return 'kept' // never untick what the page or the user ticked
    target.click()
    return target.checked ? 'filled' : 'the page did not accept the tick'
  }
  if (target.value.trim()) return 'kept'
  target.focus()
  setNativeValue(target, value)
  fire(target)
  // Read back. Masked inputs may reformat the value ("+44 7000 000000" -> "07000 000000"), which is
  // fine; a field the page emptied again (validation, a widget that needs real typing) is not filled.
  return target.value.trim() ? 'filled' : 'the page cleared the value'
}

export async function applyFills(fills: Fill[], targets: Map<string, FieldTarget>): Promise<ApplyResult> {
  const result: ApplyResult = { filled: [], kept: [], failed: [] }
  for (const fill of fills) {
    const target = targets.get(fill.id)
    if (!target) {
      result.failed.push({ id: fill.id, reason: 'field disappeared' })
      continue
    }
    try {
      const outcome = await applyOne(target, fill.value)
      if (outcome === 'filled') result.filled.push(fill.id)
      else if (outcome === 'kept') result.kept.push(fill.id)
      else result.failed.push({ id: fill.id, reason: outcome })
    } catch (e) {
      result.failed.push({ id: fill.id, reason: e instanceof Error ? e.message : String(e) })
    }
  }
  return result
}
