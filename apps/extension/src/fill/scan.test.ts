import { beforeEach, describe, expect, it, vi } from 'vitest'
import { scanFields } from './scan'
import { applyFills } from './apply'

// jsdom has no layout: every element would look invisible.
beforeEach(() => {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({ width: 100, height: 20 } as DOMRect)
  document.body.innerHTML = `
    <form>
      <label for="em">Email address *</label><input id="em" type="email" autocomplete="email">
      <div class="field"><span id="ph-l">Phone</span><input aria-labelledby="ph-l" type="tel"></div>
      <label>LinkedIn Profile <input name="li"></label>
      <div class="field"><label for="why">Why do you want to work here?</label><textarea id="why"></textarea></div>
      <label for="cty">Country</label>
      <select id="cty"><option value="">Select…</option><option value="IN">India</option><option value="US">United States</option></select>
      <fieldset><legend>Will you require visa sponsorship?</legend>
        <label><input type="radio" name="spon" value="y"> Yes</label>
        <label><input type="radio" name="spon" value="n"> No</label>
      </fieldset>
      <label><input type="checkbox" name="tos"> I agree to the privacy policy</label>
      <input type="hidden" name="csrf" value="x"><input type="file" name="resume"><button type="submit">Submit</button>
      <input name="done" value="already typed">
    </form>`
})

describe('scanFields', () => {
  it('describes each field by the question it asks', () => {
    const { fields } = scanFields()
    const byLabel = Object.fromEntries(fields.map((f) => [f.label, f]))
    expect(fields.map((f) => f.kind)).toEqual(['email', 'tel', 'text', 'textarea', 'select', 'checkbox', 'text', 'radio'])
    expect(byLabel['Email address'].autocomplete).toBe('email') // the required-star is trimmed
    expect(byLabel['Phone'].kind).toBe('tel')
    expect(byLabel['LinkedIn Profile'].name).toBe('li')
    expect(byLabel['Country'].options).toEqual(['Select…', 'India', 'United States'])
    expect(byLabel['Will you require visa sponsorship?'].options).toEqual(['Yes', 'No'])
  })
})

describe('applyFills', () => {
  it('writes values, picks options, and never overwrites what the user typed', async () => {
    const { fields, targets } = scanFields()
    const id = (label: string) => fields.find((f) => f.label === label)!.id
    const result = await applyFills(
      [
        { id: id('Email address'), value: 'ada@example.com' },
        { id: id('Country'), value: 'India' },
        { id: id('Will you require visa sponsorship?'), value: 'No' },
        { id: fields.find((f) => f.name === 'done')!.id, value: 'replacement' },
        { id: id('Phone'), value: '+44 7000 000000' },
        { id: id('Country'), value: 'Atlantis' },
      ],
      targets,
    )
    expect((document.getElementById('em') as HTMLInputElement).value).toBe('ada@example.com')
    expect((document.getElementById('cty') as HTMLSelectElement).value).toBe('IN')
    expect((document.querySelector('input[value="n"]') as HTMLInputElement).checked).toBe(true)
    expect((document.querySelector('input[name="done"]') as HTMLInputElement).value).toBe('already typed')
    expect(result.filled).toHaveLength(4)
    expect(result.kept).toEqual([fields.find((f) => f.name === 'done')!.id, id('Country')]) // the second Country write: already set
  })

  it('fires input and change events so framework-controlled inputs see the value', async () => {
    const { fields, targets } = scanFields()
    const email = document.getElementById('em') as HTMLInputElement
    const seen: string[] = []
    email.addEventListener('input', () => seen.push('input'))
    email.addEventListener('change', () => seen.push('change'))
    await applyFills([{ id: fields[0].id, value: 'ada@example.com' }], targets)
    expect(seen).toEqual(['input', 'change'])
  })
})

describe('custom widgets', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <form>
        <fieldset><legend>Which languages can you work in?</legend>
          <label><input type="checkbox" name="lang" value="en"> English</label>
          <label><input type="checkbox" name="lang" value="hi"> Hindi</label>
          <label><input type="checkbox" name="lang" value="ta"> Tamil</label>
        </fieldset>
        <div><span id="sp">Will you require visa sponsorship?</span>
          <div role="group" aria-labelledby="sp"><button type="button" aria-pressed="false">Yes</button><button type="button" aria-pressed="false">No</button></div>
        </div>
        <div><button type="button">Back</button><button type="button">Next</button></div>
        <label id="cl" for="ci">Country</label>
        <div class="select-container"><input id="ci" role="combobox" aria-controls="cm" aria-labelledby="cl"><div id="cm" role="listbox"></div></div>
      </form>`
    const input = document.getElementById('ci') as HTMLInputElement
    const menu = document.getElementById('cm')!
    const all = ['India', 'Indonesia', 'United States']
    const render = () => {
      menu.innerHTML = ''
      for (const c of all.filter((c) => c.toLowerCase().includes(input.value.toLowerCase()))) {
        const o = document.createElement('div')
        o.setAttribute('role', 'option')
        o.textContent = c
        o.addEventListener('mousedown', () => { input.value = c; menu.innerHTML = '' })
        menu.appendChild(o)
      }
    }
    input.addEventListener('mousedown', render)
    input.addEventListener('input', render)
    for (const b of document.querySelectorAll('[role="group"] button')) {
      b.addEventListener('click', () => b.parentElement!.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))))
    }
  })

  it('reads a checkbox group, an answer-button group and a combobox as one question each', async () => {
    const { fields, targets } = scanFields()
    const { harvestComboOptions } = await import('./scan')
    await harvestComboOptions(fields, targets)
    const byKind = Object.fromEntries(fields.map((f) => [f.kind, f]))
    expect(fields.map((f) => f.kind).sort()).toEqual(['checkboxes', 'combobox', 'radio']) // Back/Next are not answers
    expect(byKind.checkboxes.label).toBe('Which languages can you work in?')
    expect(byKind.checkboxes.options).toEqual(['English', 'Hindi', 'Tamil'])
    expect(byKind.radio.label).toBe('Will you require visa sponsorship?')
    expect(byKind.radio.options).toEqual(['Yes', 'No'])
    expect(byKind.combobox.label).toBe('Country')
    expect(byKind.combobox.options).toEqual(['India', 'Indonesia', 'United States'])
  })

  it('ticks every named option, presses the answer button and picks the combobox option', async () => {
    const { fields, targets } = scanFields()
    const id = (kind: string) => fields.find((f) => f.kind === kind)!.id
    const result = await applyFills(
      [
        { id: id('checkboxes'), value: 'English\nTamil' },
        { id: id('radio'), value: 'No' },
        { id: id('combobox'), value: 'India' },
      ],
      targets,
    )
    const ticked = Array.from(document.querySelectorAll<HTMLInputElement>('input[name="lang"]')).filter((c) => c.checked).map((c) => c.value)
    expect(ticked).toEqual(['en', 'ta'])
    expect(Array.from(document.querySelectorAll('button')).find((b) => b.textContent === 'No')!.getAttribute('aria-pressed')).toBe('true')
    expect((document.getElementById('ci') as HTMLInputElement).value).toBe('India')
    expect(result.failed).toEqual([])
    expect(result.filled).toHaveLength(3)
  })

  it('clears half-typed combobox text when no option matches', async () => {
    const { fields, targets } = scanFields()
    const result = await applyFills([{ id: fields.find((f) => f.kind === 'combobox')!.id, value: 'Atlantis' }], targets)
    expect(result.failed[0].reason).toMatch(/no option matches/)
    expect((document.getElementById('ci') as HTMLInputElement).value).toBe('')
  })
})
