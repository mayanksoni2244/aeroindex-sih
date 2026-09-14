/**
 * Headline minus Core — the surge measure.
 *
 * WHY THIS CHART IS THE POINT OF THE PROJECT
 * The dual-audience index is the differentiator: Headline APIx (tax-inclusive,
 * every observation) for MoSPI/NSO, Core APIx (surge-flagged observations
 * excluded) for RBI. The claim attached to that pair is that the GAP between
 * them is a direct measurement of demand-driven surge — the thing a consumer
 * price index has to capture and a monetary-policy read has to see through.
 *
 * Until this component existed, that claim was written in prose on three cards
 * and plotted nowhere. A differentiator asserted but never shown is a slide, not
 * a finding. This draws it.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 * It does not smooth, fit, or annualise the gap. It does not draw a zero line
 * and call a small positive gap a surge. The bars are the arithmetic difference
 * of two published index values, on the days where both exist — nothing is
 * inferred for a day where Core is absent, because a day whose every observation
 * was surge-flagged has no Core point by construction, and interpolating one
 * would invent the very reading the chart is meant to justify.
 *
 * Reads only `points` and `counterpart` already loaded by App. No new request,
 * so this cannot affect the Live/Sandbox separation.
 */
import React from 'react'
import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

function GapTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  return (
    <div className="rounded-md border border-slate-300 bg-white p-3 text-xs shadow-lg">
      <div className="font-semibold text-slate-900">{p.date}</div>
      <div className="mt-1 font-mono text-lg tabular-nums">
        {p.gap > 0 ? '+' : ''}
        {p.gap.toFixed(2)}
      </div>
      <div className="mt-1 text-slate-600">index points, Headline − Core</div>
      <div className="mt-2 border-t border-slate-200 pt-2 text-slate-600">
        <div>
          Headline <span className="font-mono">{p.headline.toFixed(2)}</span>
        </div>
        <div>
          Core <span className="font-mono">{p.core.toFixed(2)}</span>
        </div>
        <div className="mt-1 text-slate-500">
          {p.gap > 0
            ? 'Headline sits above Core: surge-flagged observations are pulling the all-in index up.'
            : p.gap < 0
              ? 'Core sits above Headline. Worth a look — this is the opposite of the usual direction.'
              : 'The two series agree exactly on this day.'}
        </div>
      </div>
    </div>
  )
}

export default function SurgeGap({
  points,
  counterpart,
  series,
  measure,
  height = 240,
}) {
  // Only days present in BOTH series produce a gap. See the file header: a
  // missing Core point is a day every observation was flagged, which is a fact
  // about the day, not a zero.
  const coreByDate = new Map(
    (counterpart || []).map((p) => [p.index_date, p.index_value]),
  )
  const data = (points || [])
    .filter((p) => coreByDate.has(p.index_date))
    .map((p) => {
      const headline = series === 'headline' ? p.index_value : coreByDate.get(p.index_date)
      const core = series === 'headline' ? coreByDate.get(p.index_date) : p.index_value
      return {
        date: p.index_date,
        headline,
        core,
        gap: Number((headline - core).toFixed(4)),
      }
    })

  const unmatched = (points?.length || 0) - data.length

  if (!data.length) {
    return (
      <p className="py-8 text-center text-sm text-slate-500">
        No day carries both a Headline and a Core value, so no gap exists to
        plot. This is not a gap of zero.
      </p>
    )
  }

  const maxAbs = Math.max(0.5, ...data.map((d) => Math.abs(d.gap)))

  return (
    <div className="space-y-3">
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11, fill: '#64748b' }}
            minTickGap={24}
          />
          <YAxis
            tick={{ fontSize: 11, fill: '#64748b' }}
            width={64}
            domain={[-maxAbs * 1.3, maxAbs * 1.3]}
            tickFormatter={(v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}`}
            label={{
              value: 'Headline − Core',
              angle: -90,
              position: 'insideLeft',
              style: { fontSize: 11, fill: '#64748b', textAnchor: 'middle' },
            }}
          />
          <ReferenceLine y={0} stroke="#94a3b8" />
          <Tooltip content={<GapTooltip />} cursor={{ fill: '#f1f5f9' }} />
          <Bar dataKey="gap" isAnimationActive={false}>
            {data.map((d) => (
              <Cell
                key={d.date}
                // Red above zero (surge lifting Headline), blue below. A gap of
                // exactly zero is grey rather than either, because colouring it
                // as a small surge would be reading signal into arithmetic.
                fill={
                  d.gap > 0 ? '#dc2626' : d.gap < 0 ? '#2563eb' : '#94a3b8'
                }
                fillOpacity={0.85}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
        <span className="font-semibold uppercase tracking-wide text-slate-500">
          Key
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-3 rounded-sm"
            style={{ backgroundColor: '#dc2626' }}
          />
          Headline above Core — surge flagged that day
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-3 rounded-sm"
            style={{ backgroundColor: '#2563eb' }}
          />
          Core above Headline
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-3 rounded-sm"
            style={{ backgroundColor: '#94a3b8' }}
          />
          the two agree exactly
        </span>
        <span className="text-slate-500">
          both series present on {data.length} day{data.length === 1 ? '' : 's'}
        </span>
        {unmatched > 0 && (
          <span className="text-slate-500">
            · no counterpart on {unmatched} day{unmatched === 1 ? '' : 's'} —
            omitted, not zero-filled
          </span>
        )}
      </div>

      <p className="text-xs leading-relaxed text-slate-600">
        The gap is the surge measure: it is the part of the all-in index that
        Core drops. Core excludes observations flagged as festival/high-demand
        windows and Tukey statistical outliers; Headline keeps every observation.
        Both are built from the same matched cells, so the difference is the
        flagging rule and nothing else.
      </p>

      {data.length === 1 && (
        <p className="caveat border-slate-400 bg-slate-50 text-slate-700">
          One day plotted, so the gap is zero by construction rather than by
          measurement: with a single observation the base period is that day, and
          both series are pinned to 100.0 against it. The bar will carry
          information once a second cycle lands and the current period stops
          being the base period. Read nothing into it until then.
        </p>
      )}
    </div>
  )
}
