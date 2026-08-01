# FetchForge — Design Context

Source of truth for `fetchforge/index.html`. If a change conflicts with this file,
the conflict gets raised rather than silently resolved.

## Surface type

**Product**, not brand. A single-page local tool that one person runs on their own
machine at `localhost:8765`, usually to start a long job and then leave it alone.

The consequence: motion and personality are budgeted, not free. This surface is
read while something is encoding, so anything that moves has to be reporting
state. The exception is job completion, which is rare enough to earn delight.

## Audience and tone

One power user who knows what a CQ value is. Three adjectives:

**Instrumented. Dense. Unfussy.**

It should read like a piece of equipment — labelled, monospaced, honest about
numbers — not like a consumer app that hides its settings.

## Palette

Cool violet-tinted neutrals under a warm amber accent. Nothing is pure black or
pure white.

| Token | Hex | Role |
|---|---|---|
| `--bg` | `#111318` | Page |
| `--surface` | `#1a1d25` | Cards |
| `--surface2` | `#22262f` | Inputs, nested panels |
| `--border` | `#2d3240` | Default edge |
| `--border2` | `#3a3f50` | Hover / emphasis edge |
| `--accent` | `#e8a023` | Primary — amber. Encoding, CTAs, focus rings |
| `--accent2` | `#ef5350` | Destructive / error |
| `--accent3` | `#4dd0a0` | Success, downloading |
| `--accent4` | `#64b5f6` | Informational |
| `--text` | `#e8e4df` | Body |
| `--text2` | `#a5a0b2` | Secondary |
| `--muted` | `#8e8b9b` | Micro-labels, metadata |

**The neutral ramp is contrast-checked, not chosen by eye.** Worst case is text on
`--surface2`: `--text` 12.0:1, `--text2` 6.0:1, `--muted` 4.6:1. Do not darken any
of the three without re-running the ratios against all three backgrounds.

Fills carry dark text (`#111`), never white — `#fff` on `--accent2` is only 3.5:1.

## Type

- **Headings / UI:** Outfit (400–800)
- **Numbers, labels, paths, anything instrument-like:** JetBrains Mono (400–500)

Both from Google Fonts with `display=swap`. Tracking is size-specific: `-0.5px` on
the 28px logo, `0` on body, `+1.5px` on uppercase mono micro-labels.

**11px is the floor for any functional text**, including mono micro-labels. Body
copy is 15px, secondary 13–14px.

## Motion

- Easing tokens only: `--ease-out` for enter/exit, `--ease-in-out` for on-screen
  movement. Never `ease-in` on UI.
- Name the properties. Never `transition: all`.
- Animate `transform` and `opacity`. The progress bars' `width` is a deliberate,
  documented exception.
- Hover *travel* lives inside `@media (hover: hover) and (pointer: fine)`.
- Coloured glows are allowed **only** where they signal phase, and each glow must
  match the colour of the element it lights.
- `prefers-reduced-motion` means gentler, not off: fades, colour changes and
  progress widths stay; travel, scaling, confetti and the shimmer go. The
  indeterminate bar keeps animating on opacity, because that loop is the message.
- Celebration on job completion is in budget — it fires once per run.

## Layout

- 900px max content width; body copy capped around 68ch.
- One level of containment. Cards do not nest in cards.
- Stacking uses the scale only: `--z-sticky: 30`, `--z-overlay: 50`.

## Anti-references

- **Generic SaaS dashboard.** No Inter, no system-ui, no rounded-square icon tile
  above every heading.
- **Emoji standing in for icons.** Icons are inline SVG so they inherit
  `currentColor` and track the type ramp. Emoji are colour glyphs from a system
  font: they ignore `color`, so they cannot be tinted to match state, and they
  render and align differently per platform. The current file still violates this
  (`➕ 📄 📁 🎬 🎵 ✕ ▶`) — tracked in #54, and the rule is recorded here so the
  violation reads as debt rather than as intent.
- **The "AI product" gradient.** No purple-to-blue, no teal-to-purple. A retired
  violet palette left real traces here (an amber dot glowing violet, a converting
  bar that ramped through `#6c4ce0`); those are fixed, and new ones are bugs.
- **Toy-like.** No bounce or elastic easing on chrome, no rainbow hue-rotate, no
  decorative pulsing that isn't reporting live state.
- **Consumer-app minimalism.** Do not hide the numbers to look calmer. Density is
  the point.

## Known waivers

Eight detector rules are waived for `fetchforge/index.html` in
`.impeccable/config.json`, each with a written reason. Read those before
"fixing" a glow, a `transition: width`, or an `<img>` with no `src`.
