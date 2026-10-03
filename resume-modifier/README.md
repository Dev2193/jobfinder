# Resume Modifier

Manual, on-demand resume tailoring workflow, run through Claude. Separate from
`application-engine`, which submits the original resume PDF unmodified — this
workflow never touches that pipeline.

## Source of truth

`master-resume.tex` — a one-page LaTeX resume (Jake's Resume template). This is
the only file that should be treated as canonical; re-export it from Overleaf
and replace this copy whenever the real resume changes.

## How a tailored version gets made

1. Check `examples/` first for a resume already tailored to a substantially
   similar role — reuse/relink instead of regenerating when there's a real match.
2. Otherwise, start from the current `master-resume.tex`.
3. Edit **only bullet-level wording** inside existing `\resumeItem{...}` entries.
   Section headers, section order, and the full set/order of `\resumeSubheading`
   entries never change — nothing is added, removed, or reordered.
4. No fabrication — only reposition or reword content genuinely already in the
   resume. No invented skills, metrics, or experience.
5. Must compile to exactly **one page**. If tailored wording overflows, tighten
   phrasing rather than cutting an entry or section.
6. Save the result under `examples/{company}-{role}/resume.tex` (+ compiled
   `resume.pdf`), and add an entry below.

Compiled with [tectonic](https://tectonic-typesetting.github.io/) (no local
TeX install required): `tectonic resume.tex`.

## Tailored examples

| Company / Role | Notes |
|---|---|
| `examples/mckinsey-quantumblack-swe-intern` | McKinsey / QuantumBlack — Software Engineering AI Intern. Leaned into AI-tooling judgment, cross-functional collaboration, end-to-end data pipeline work. |
| `examples/john-deere-ux-research` | John Deere — Part-Time Student, UX Research. Leaned into HCI/usability research (NASA-TLX), patient/stakeholder interview and iteration work. |

Also produced (not stored here — lives in Google Docs from an earlier version
of this workflow, before the source of truth moved to LaTeX/Overleaf):
General Motors — Digital Products Software Engineer/Test Engineer Intern.

## Cover letters

On request only, not automatic. Same no-fabrication rule. Tone matches the
user's direct, concise, low-fluff style — not literal casual phrasing.
