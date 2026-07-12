import type { ReactNode } from 'react'

/* A minimal, dependency-free markdown renderer for the edge playbook prose and
   (Task 19) the analysis-summary md — the tech stack is "Existing only", no
   markdown library. It FORMATS the code-authored md; it never fabricates
   content — any construct it does not recognize falls through as literal text
   (the honesty rule). React escapes all text, so there is no
   dangerouslySetInnerHTML and no injection surface.

   Scope = the constructs the edge/*.md files actually use: ATX headings,
   blockquotes, fenced + inline code, unordered lists (with wrapped-line
   continuation), thematic breaks, and paragraphs, with inline bold / italic /
   code. Inline parsing is a PRIORITY SCAN, not a regex sweep (the old sweep
   garbled real prose): code spans first (contents literal — a backticked
   `market_trend=bear` inside **bold** renders bold+code, never raw backticks),
   then **bold** (recursing so emphasis/code nest), then *italic* and _italic_ —
   the underscore form gated on WORD BOUNDARIES so a bare identifier like
   `vol_tier=high` never italicizes a mid-sentence run.

   Two behaviors are OPT-OUT via props (default the Playbooks behavior):
   `stripFrontmatter` drops a leading YAML `---` fence (metadata, surfaced
   separately — not prose), and `strikeFalsifiedH2` strikes content under an H2
   whose title starts with "Falsified" (retired ideas, kept for the record).
   Task 19's summary md has neither, so it passes both false. */

function dropFrontmatter(md: string): string {
  const lines = md.split('\n')
  if (lines[0]?.trim() !== '---') return md
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') return lines.slice(i + 1).join('\n')
  }
  return md // an unterminated fence is not a fence — render verbatim
}

const WORD = /[A-Za-z0-9]/

/** An opening `_` is legal only off a word boundary (prev char not alphanumeric)
 * and onto non-space content — so `market_trend` never opens emphasis. */
function opensUnderscore(text: string, i: number): boolean {
  const prev = i > 0 ? text[i - 1] : ''
  const next = i + 1 < text.length ? text[i + 1] : ''
  return (prev === '' || !WORD.test(prev)) && next !== '' && !/\s/.test(next)
}

/** A closing `_` is legal only onto a word boundary (next char not alphanumeric)
 * and off non-space content — so the second `_` of `vol_tier` never closes. */
function closesUnderscore(text: string, i: number): boolean {
  const prev = i > 0 ? text[i - 1] : ''
  const next = i + 1 < text.length ? text[i + 1] : ''
  return (next === '' || !WORD.test(next)) && prev !== '' && !/\s/.test(prev)
}

/** Inline formatting as a single left-to-right scan. Emphasis contents recurse;
 * code-span contents are LITERAL (no nested parsing). Unbalanced markers fall
 * through as plain text — never fabricated structure. */
function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let buf = ''
  let key = 0
  const flush = () => {
    if (buf !== '') {
      nodes.push(buf)
      buf = ''
    }
  }
  let i = 0
  const n = text.length
  while (i < n) {
    const ch = text[i]

    // code span: `...` — contents literal, highest priority
    if (ch === '`') {
      const end = text.indexOf('`', i + 1)
      if (end > i) {
        flush()
        nodes.push(
          <code key={key++} className="md-code">
            {text.slice(i + 1, end)}
          </code>,
        )
        i = end + 1
        continue
      }
    }

    // bold: **...** — recurse so code/emphasis nest inside
    if (ch === '*' && text[i + 1] === '*') {
      const end = text.indexOf('**', i + 2)
      if (end > i + 1) {
        flush()
        nodes.push(<strong key={key++}>{renderInline(text.slice(i + 2, end))}</strong>)
        i = end + 2
        continue
      }
    }

    // italic: *...* (a single star, not the start of **)
    if (ch === '*' && text[i + 1] !== '*') {
      const end = text.indexOf('*', i + 1)
      if (end > i + 1) {
        flush()
        nodes.push(<em key={key++}>{renderInline(text.slice(i + 1, end))}</em>)
        i = end + 1
        continue
      }
    }

    // italic: _..._ — both ends gated on word boundaries
    if (ch === '_' && opensUnderscore(text, i)) {
      let j = i + 1
      let end = -1
      while (j < n) {
        const e = text.indexOf('_', j)
        if (e === -1) break
        if (closesUnderscore(text, e)) {
          end = e
          break
        }
        j = e + 1
      }
      if (end > i + 1) {
        flush()
        nodes.push(<em key={key++}>{renderInline(text.slice(i + 1, end))}</em>)
        i = end + 1
        continue
      }
    }

    buf += ch
    i++
  }
  flush()
  return nodes
}

const isListItem = (l: string): boolean => /^\s*[-*]\s+/.test(l)
const isHeading = (l: string): boolean => /^#{1,6}\s+/.test(l)
const isQuote = (l: string): boolean => /^\s*>/.test(l)
const isRule = (l: string): boolean => /^(-{3,}|\*{3,})\s*$/.test(l)
const isFence = (l: string): boolean => l.trimStart().startsWith('```')

export function Markdown({
  md,
  stripFrontmatter = true,
  strikeFalsifiedH2 = true,
}: {
  md: string
  /** Drop a leading YAML `---` fence (Playbooks: true; a summary opening with
   * `---` must NOT be swallowed, so Task 19 passes false). */
  stripFrontmatter?: boolean
  /** Strike content under an H2 titled "Falsified…" (Playbooks: true; Task 19
   * passes false so a stray "Falsified" summary heading is not struck). */
  strikeFalsifiedH2?: boolean
}) {
  // Normalize EOLs first: a stray \r would break the anchored block regexes
  // (a heading line "## Falsified\r" would render as text and silently skip the
  // strike). reflect._edge_text normalizes today, but Task 19's md may not.
  const normalized = md.replace(/\r\n?/g, '\n')
  const source = stripFrontmatter ? dropFrontmatter(normalized) : normalized
  const lines = source.split('\n')
  const blocks: ReactNode[] = []
  let struck = false // inside the "Falsified / retired" section
  let key = 0
  let i = 0
  const cls = (base: string) => (struck ? `${base} md-struck` : base)

  while (i < lines.length) {
    const line = lines[i]
    if (line.trim() === '') {
      i++
      continue
    }

    // fenced code block — inner lines are LITERAL: no heading parsing, no
    // strike toggle (an Opus author could emit a ``` block containing "## X").
    if (isFence(line)) {
      i++
      const code: string[] = []
      while (i < lines.length && !isFence(lines[i])) {
        code.push(lines[i])
        i++
      }
      if (i < lines.length) i++ // consume the closing fence
      blocks.push(
        <pre key={key++} className={cls('md-pre')}>
          <code>{code.join('\n')}</code>
        </pre>,
      )
      continue
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading !== null) {
      const level = heading[1].length
      const text = heading[2].trim()
      if (level === 2 && strikeFalsifiedH2) struck = /^falsified/i.test(text)
      const Tag = `h${Math.min(level + 2, 6)}` as 'h3' | 'h4' | 'h5' | 'h6'
      blocks.push(
        <Tag key={key++} className={cls('md-h')}>
          {renderInline(text)}
        </Tag>,
      )
      i++
      continue
    }

    if (isQuote(line)) {
      const buf: string[] = []
      while (i < lines.length && isQuote(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''))
        i++
      }
      blocks.push(
        <blockquote key={key++} className={cls('md-quote')}>
          {renderInline(buf.join(' '))}
        </blockquote>,
      )
      continue
    }

    if (isRule(line)) {
      blocks.push(<hr key={key++} className="md-hr" />)
      i++
      continue
    }

    if (isListItem(line)) {
      const items: string[] = []
      while (i < lines.length && isListItem(lines[i])) {
        let text = lines[i].replace(/^\s*[-*]\s+/, '')
        i++
        // absorb wrapped continuation lines (indented, non-blank, not a new item)
        while (
          i < lines.length &&
          lines[i].trim() !== '' &&
          /^\s+/.test(lines[i]) &&
          !isListItem(lines[i])
        ) {
          text += ' ' + lines[i].trim()
          i++
        }
        items.push(text)
      }
      blocks.push(
        <ul key={key++} className={cls('md-ul')}>
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it)}</li>
          ))}
        </ul>,
      )
      continue
    }

    // paragraph — gather until a blank or a block-starting line
    const para: string[] = []
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !isHeading(lines[i]) &&
      !isQuote(lines[i]) &&
      !isListItem(lines[i]) &&
      !isRule(lines[i]) &&
      !isFence(lines[i])
    ) {
      para.push(lines[i])
      i++
    }
    blocks.push(
      <p key={key++} className={cls('md-p')}>
        {renderInline(para.join(' '))}
      </p>,
    )
  }

  return <div className="md">{blocks}</div>
}
