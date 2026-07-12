import type { ReactNode } from 'react'

/* A minimal, dependency-free markdown renderer for the edge playbook prose
   (Task 18; the tech stack is "Existing only" — no markdown library). It
   FORMATS the code-authored md; it never fabricates content — any construct it
   does not recognize falls through as literal text (the honesty rule). The
   backend serves the file VERBATIM; this renders that text as markdown.

   Scope = the constructs the edge/*.md files actually use: ATX headings,
   blockquotes, unordered lists (with wrapped-line continuation), thematic
   breaks, and paragraphs, with inline bold / italic / code. React escapes all
   text, so there is no dangerouslySetInnerHTML and no injection surface.

   Two deliberate rendering choices, documented here so they are not mistaken for
   silent edits:
   - the leading YAML frontmatter fence is STRIPPED — it is metadata (surfaced
     separately as the reflection-state row), not prose, and its numbers drive
     nothing on screen (those come from the sidecar);
   - content under an H2 whose title starts with "Falsified" renders
     struck-through (the backend also pre-extracts that body; tracking the
     section here lets the whole file render in one in-place pass). */

function stripFrontmatter(md: string): string {
  const lines = md.split('\n')
  if (lines[0]?.trim() !== '---') return md
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') return lines.slice(i + 1).join('\n')
  }
  return md // an unterminated fence is not a fence — render verbatim
}

// code | **bold** | _italic_ | *italic*, matched left-to-right and non-nested:
// a bold token's inner underscores never re-parse (the North Star files carry
// `market_trend=bull` inside **bold** all the time).
const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(_[^_]+_)|(\*[^*]+\*)/g

function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let k = 0
  let m: RegExpExecArray | null
  INLINE.lastIndex = 0
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    if (m[1] !== undefined) {
      out.push(
        <code key={k++} className="md-code">
          {tok.slice(1, -1)}
        </code>,
      )
    } else if (m[2] !== undefined) {
      out.push(<strong key={k++}>{tok.slice(2, -2)}</strong>)
    } else {
      out.push(<em key={k++}>{tok.slice(1, -1)}</em>)
    }
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const isListItem = (l: string): boolean => /^\s*[-*]\s+/.test(l)
const isHeading = (l: string): boolean => /^#{1,6}\s+/.test(l)
const isQuote = (l: string): boolean => /^\s*>/.test(l)
const isRule = (l: string): boolean => /^(-{3,}|\*{3,})\s*$/.test(l)

export function Markdown({ md }: { md: string }) {
  const lines = stripFrontmatter(md).split('\n')
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

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading !== null) {
      const level = heading[1].length
      const text = heading[2].trim()
      if (level === 2) struck = /^falsified/i.test(text)
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
      !isRule(lines[i])
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
