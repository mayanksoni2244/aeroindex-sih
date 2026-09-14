/**
 * Confidence grading — how much real evidence stands behind a number.
 *
 * WHAT THIS RENDERS, AND WHAT IT REFUSES TO IMPLY
 * The API publishes a *support* grade, not a confidence interval: how many
 * real, matched, well-spread observations a value rests on. The temptation in a
 * dashboard is to draw a score out of 100 with a green ring and let a viewer
 * read it as "95% likely to be right". Two things here exist specifically to
 * block that reading:
 *
 *   1. The score is never shown without the words. `LOW` and `INDICATIVE` are
 *      printed at the same size as the number, because the number alone is the
 *      part that gets quoted out of context.
 *   2. Every grade ships with its `reasons`. The API computes those from the
 *      same lineage the provenance panel reads, so the badge and the caveat
 *      beneath it cannot drift apart.
 *
 * THE WEIGHTS COME FROM THE API
 * `grade.weights` is rendered verbatim rather than hardcoded here. If the
 * engine's weighting changes, this panel changes with it — a dashboard printing
 * weights the engine no longer uses would be two documents disagreeing about
 * one number.
 *
 * INTERACTIVITY
 * The badge is a disclosure control, not a decoration: it opens the component
 * breakdown and the reason list. Nothing is hidden behind a hover, because a
 * caveat that lives in a `title=` attribute does not survive a screenshot.
 */
import React, { useState } from 'react'

/** Grade -> appearance. `INDICATIVE` is styled as loudly as `HIGH`, on purpose. */
const GRADES = {
  HIGH: {
    chip: 'bg-emerald-100 text-emerald-900 ring-emerald-300',
    bar: 'bg-emerald-500',
    dot: 'bg-emerald-500',
  },
  MODERATE: {
    chip: 'bg-sky-100 text-sky-900 ring-sky-300',
    bar: 'bg-sky-500',
    dot: 'bg-sky-500',
  },
  LOW: {
    chip: 'bg-amber-100 text-amber-900 ring-amber-300',
    bar: 'bg-amber-500',
    dot: 'bg-amber-500',
  },
  INDICATIVE: {
    chip: 'bg-slate-200 text-slate-800 ring-slate-400',
    bar: 'bg-slate-500',
    dot: 'bg-slate-500',
  },
}

const FALLBACK = GRADES.INDICATIVE

/** What each component measures, in the words a reader needs to act on it. */
const COMPONENT_HELP = {
  observation: {
    label: 'Observation',
    help: 'How many real matched cells back this value, against the 20 a full 4-route x 5-window cycle yields.',
  },
  provenance: {
    label: 'Provenance',
    help: 'The share of contributing quotes that are real observations, rather than simulated or carried forward.',
  },
  coverage: {
    label: 'Coverage',
    help: 'The share of the basket’s original DGCA passenger weight represented. Weight, not route count — losing DEL-BOM is not the same event as losing BLR-HYD.',
  },
  breadth: {
    label: 'Breadth',
    help: 'How evenly observations are spread across the covered routes, so one heavily-sampled route cannot pass for a broad index. Normalised Shannon entropy.',
  },
}

export function gradeStyle(grade) {
  return GRADES[grade] || FALLBACK
}

/**
 * The inline chip. Reads "HIGH confidence · 100" and opens for detail.
 *
 * Rendered as a <button> rather than a <span> so it is reachable by keyboard:
 * a caveat only a mouse can open is a caveat that is missing for some readers.
 */
export function ConfidenceBadge({ confidence, onToggle, expanded = false, size = 'md' }) {
  if (!confidence) return null
  const style = gradeStyle(confidence.grade)
  const pad = size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs'

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      title="How much real evidence backs this value"
      className={`inline-flex items-center gap-2 rounded-full font-medium ring-1 transition hover:brightness-95 ${style.chip} ${pad}`}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      <span className="font-semibold">{confidence.grade}</span>
      <span className="opacity-50">confidence</span>
      <span className="font-mono tabular-nums opacity-90">
        {confidence.score.toFixed(0)}
      </span>
      <span className="opacity-60" aria-hidden="true">
        {expanded ? '▲' : '▼'}
      </span>
    </button>
  )
}

/** One component's weighted contribution, drawn to scale. */
function ComponentRow({ name, value, weight }) {
  const meta = COMPONENT_HELP[name] || { label: name, help: '' }
  // Points out of 100, not a 0..1 share: the reader is being shown how the
  // total was reached, so the column has to add up to the headline score.
  const contribution = value * weight * 100
  return (
    <div className="grid grid-cols-[9.5rem_1fr_5.5rem] items-center gap-3">
      <div
        className="text-xs font-medium text-slate-700"
        title={meta.help}
      >
        {meta.label}
        <span className="ml-1 font-mono text-[10px] font-normal text-slate-400">
          w={weight.toFixed(2)}
        </span>
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-slate-200"
        title={meta.help}
      >
        <div
          className={`h-full rounded-full ${
            value >= 0.8
              ? 'bg-emerald-500'
              : value >= 0.5
                ? 'bg-sky-500'
                : value >= 0.2
                  ? 'bg-amber-500'
                  : 'bg-slate-500'
          }`}
          style={{ width: `${Math.max(1.5, value * 100)}%` }}
        />
      </div>
      <div className="text-right font-mono text-xs tabular-nums text-slate-600">
        {(value * 100).toFixed(0)}%
        <span className="ml-1 text-[10px] text-slate-400">
          +{contribution.toFixed(1)} pts
        </span>
      </div>
    </div>
  )
}

/**
 * The full panel: score, bands, weighted components, and the reasons.
 *
 * `variant="inline"` drops the frame for use inside a card that already has one.
 */
export function ConfidencePanel({ confidence, title, scope, variant = 'card' }) {
  if (!confidence) return null

  const style = gradeStyle(confidence.grade)
  const weights = confidence.weights || {}
  const components = confidence.components || {}
  // Sorted by weight so the reader meets the biggest driver first. Falls back to
  // the documented order when the API sent no weights.
  const order = Object.keys(weights).length
    ? Object.keys(weights).sort((a, b) => weights[b] - weights[a])
    : ['observation', 'provenance', 'coverage', 'breadth']

  const shell =
    variant === 'card'
      ? 'rounded-lg border border-slate-200 bg-white p-5 shadow-sm'
      : 'rounded-md border border-slate-200 bg-slate-50 p-4'

  return (
    <div className={shell}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            {title || 'Confidence in this value'}
          </div>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-600">
            How much real, matched, well-spread observation this value rests on.
            This is a support grade, <em>not</em> a confidence interval — it does
            not say how close the value is to a population mean, because a
            four-route basket cannot support that claim.
          </p>
        </div>
        <div className="flex items-baseline gap-2">
          <span className={`stat ${style.chip.split(' ')[1]}`}>
            {confidence.score.toFixed(0)}
          </span>
          <span className="text-sm text-slate-400">/ 100</span>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ${style.chip}`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
          {confidence.grade}
        </span>
        <span className="text-xs text-slate-600">
          <strong className="font-mono">{confidence.real_cells}</strong> real
          matched cell{confidence.real_cells === 1 ? '' : 's'} of{' '}
          <strong className="font-mono">{confidence.target_cells}</strong> target
          {scope ? ` · ${scope}` : ''}
        </span>
      </div>

      {/* The bands, drawn once, so a reader learns the scale rather than
          inferring it from a single colour. */}
      <div className="mt-4">
        <div className="relative h-2 w-full overflow-hidden rounded-full bg-slate-200">
          <div
            className={`h-full ${style.bar}`}
            style={{ width: `${Math.max(2, confidence.score)}%` }}
          />
          {/* Band edges at 35 / 60 / 80, matching apix/index/confidence.py. */}
          {[35, 60, 80].map((edge) => (
            <span
              key={edge}
              className="absolute top-0 h-full w-px bg-white/80"
              style={{ left: `${edge}%` }}
              title={`band edge at ${edge}`}
            />
          ))}
        </div>
        <div className="mt-1 flex justify-between font-mono text-[10px] text-slate-400">
          <span>0 · INDICATIVE</span>
          <span>35 · LOW</span>
          <span>60 · MODERATE</span>
          <span>80 · HIGH</span>
        </div>
      </div>

      <div className="mt-5 space-y-2.5">
        <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          What the score is made of
        </div>
        {order.map((name) => (
          <ComponentRow
            key={name}
            name={name}
            value={components[name] ?? 0}
            weight={weights[name] ?? 0}
          />
        ))}
      </div>

      {confidence.reasons?.length > 0 && (
        <div className="mt-5">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Why this grade
          </div>
          <ul className="mt-2 space-y-1.5">
            {confidence.reasons.map((r) => (
              <li key={r} className="flex gap-2 text-xs leading-relaxed text-slate-700">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-400" />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/**
 * The collapsed disclosure used beside a single index value.
 *
 * Deliberately not a tooltip. The whole point is that the weakest grade on
 * screen is visible without an interaction; the interaction only adds detail.
 */
export function ConfidenceDisclosure({ confidence, title, scope }) {
  const [open, setOpen] = useState(false)
  if (!confidence) return null
  return (
    <div className="space-y-3">
      <ConfidenceBadge
        confidence={confidence}
        expanded={open}
        onToggle={() => setOpen((v) => !v)}
      />
      {open && (
        <ConfidencePanel
          confidence={confidence}
          title={title}
          scope={scope}
          variant="inline"
        />
      )}
    </div>
  )
}

/** Chart legend key for the per-point grade ring. */
export const GRADE_COLOURS = {
  HIGH: '#059669',
  MODERATE: '#0284c7',
  LOW: '#d97706',
  INDICATIVE: '#64748b',
}
