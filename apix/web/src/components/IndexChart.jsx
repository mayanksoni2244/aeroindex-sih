/**
 * The index trend chart.
 *
 * PER-POINT PROVENANCE ENCODING
 * The line is one colour, but each dot is coloured by what backed that
 * particular day: green for a point with live observations, amber for one that
 * is entirely simulated, violet where imputation dominates. A viewer scanning
 * the chart can see which stretch of the series is real without reading a
 * table — and cannot mistake a simulated run-up for an observed one.
 *
 * The alternative — a single caveat under the chart — fails the moment someone
 * crops the screenshot.
 */
import React from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { GRADE_COLOURS } from './Confidence.jsx'
import { PROVENANCE_COLOURS } from './Provenance.jsx'

/** Which provenance dominates one point. Ties fall to the less flattering. */
export function dominantSource(lineage) {
  if (!lineage) return 'simulated'
  const {
    live_pct: live = 0,
    live_limited_pct: limited = 0,
    imputed_pct: imputed = 0,
  } = lineage
  if (imputed >= 50) return 'imputed'
  if (live > 0) return 'live'
  // A day backed only by a Tier-1.5 source is still a day of real observed
  // fares. Colouring it amber alongside the generator would have understated
  // the series in the one place the eye reads provenance without a table.
  if (limited > 0) return 'live_limited'
  return 'simulated'
}

/**
 * One dot, encoding two independent things at once:
 *   fill  = where the fares came from (live / rate-capped / simulated / carried)
 *   ring  = how much evidence the point rests on (its confidence grade)
 *
 * Two channels rather than one because they can disagree, and the disagreement
 * is the interesting case: a point can be 100% live and still be thinly
 * supported if only one route reported. A single colour would have to pick one
 * of those facts and hide the other.
 */
function ProvenanceDot(props) {
  const { cx, cy, payload } = props
  if (cx === null || cy === null || cx === undefined || cy === undefined) {
    return null
  }
  const kind = dominantSource(payload.lineage)
  const gradeRing = payload.grade ? GRADE_COLOURS[payload.grade] : null
  const selected = payload.isSelected
  // Ring colour, in priority order: selection beats a coverage warning beats the
  // confidence grade. Each is a real statement about the point, and the one that
  // changes how the point must be read wins the ring.
  const ring = selected
    ? '#0f172a'
    : payload.lowCoverage
      ? PROVENANCE_COLOURS.surge
      : gradeRing
  return (
    <g>
      {selected && (
        <circle
          cx={cx}
          cy={cy}
          r={9}
          fill="none"
          stroke="#0f172a"
          strokeWidth={1}
          strokeDasharray="2 2"
        />
      )}
      <circle
        cx={cx}
        cy={cy}
        r={payload.lowCoverage ? 5 : 4}
        fill={PROVENANCE_COLOURS[kind]}
        stroke={ring || '#ffffff'}
        strokeWidth={ring ? 2 : 1}
      />
    </g>
  )
}

function IndexTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  const l = p.lineage || {}
  const c = p.confidence

  return (
    <div className="rounded-md border border-slate-300 bg-white p-3 text-xs shadow-lg">
      <div className="font-semibold text-slate-900">{p.date}</div>
      <div className="mt-1 font-mono text-lg tabular-nums">
        {p.value.toFixed(2)}
      </div>
      <div className="mt-2 border-t border-slate-200 pt-2 text-slate-600">
        <div>
          <span style={{ color: PROVENANCE_COLOURS.live }}>
            {(l.live_pct ?? 0).toFixed(0)}% live
          </span>
          {(l.live_limited_pct ?? 0) > 0 && (
            <>
              {' · '}
              <span style={{ color: PROVENANCE_COLOURS.live_limited }}>
                {l.live_limited_pct.toFixed(0)}% rate-capped live
              </span>
            </>
          )}
          {' · '}
          <span style={{ color: PROVENANCE_COLOURS.simulated }}>
            {(l.simulated_pct ?? 0).toFixed(0)}% simulated
          </span>
          {' · '}
          <span style={{ color: PROVENANCE_COLOURS.imputed }}>
            {(l.imputed_pct ?? 0).toFixed(0)}% carried forward
          </span>
        </div>
        <div className="mt-1">
          {l.quote_count ?? 0} quotes across {(l.routes_covered || []).length}{' '}
          routes
          {l.cells_matched != null && ` · ${l.cells_matched} matched cells`}
        </div>
        {c && (
          <div className="mt-1 flex items-center gap-1.5">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: GRADE_COLOURS[c.grade] || '#64748b' }}
            />
            <span className="font-semibold">{c.grade}</span>
            <span className="text-slate-500">
              confidence {c.score.toFixed(0)}/100
            </span>
          </div>
        )}
        {p.lowCoverage && (
          <div className="mt-1 font-semibold text-red-700">
            Low coverage — fewer routes than the basket requires.
          </div>
        )}
        <div className="mt-1 text-slate-400">Click the dot to inspect this day.</div>
      </div>
    </div>
  )
}

export default function IndexChart({
  points,
  compare,
  height = 420,
  onSelectDate,
  selectedDate,
}) {
  if (!points?.length) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-slate-500">
        No index points in this window. Nothing is plotted, because there is
        nothing to plot — this is not a flat line at zero.
      </div>
    )
  }

  // Aligned by date, not by array position. Headline and Core need not have the
  // same number of points — a cycle in which every observation was flagged as
  // high-demand yields a Headline point and no Core point — and zipping them
  // positionally would silently plot one day's Core against another day's
  // Headline. A missing counterpart is a gap in the dashed line, which is what
  // it is.
  const compareByDate = new Map(
    (compare || []).map((p) => [p.index_date, p.index_value]),
  )

  const data = points.map((p) => ({
    date: p.index_date,
    value: p.index_value,
    lineage: p.lineage,
    confidence: p.confidence,
    grade: p.confidence?.grade,
    lowCoverage: p.lineage?.low_coverage === true,
    isSelected: selectedDate === p.index_date,
    compare: compareByDate.has(p.index_date)
      ? compareByDate.get(p.index_date)
      : null,
  }))

  const values = data.flatMap((d) =>
    [d.value, d.compare].filter((v) => v !== null),
  )
  const pad = Math.max(1, (Math.max(...values) - Math.min(...values)) * 0.15)

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart
        data={data}
        margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
        onClick={(state) => {
          // `activeLabel` is the x value of the nearest point to the click,
          // which is what a user means by "that day".
          if (state?.activeLabel) onSelectDate?.(state.activeLabel)
        }}
        style={onSelectDate ? { cursor: 'pointer' } : undefined}
      >
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 12, fill: '#64748b' }}
          minTickGap={24}
        />
        <YAxis
          domain={[
            Math.floor(Math.min(...values) - pad),
            Math.ceil(Math.max(...values) + pad),
          ]}
          tick={{ fontSize: 12, fill: '#64748b' }}
          width={68}
          label={{
            value: 'index (base = 100)',
            angle: -90,
            position: 'insideLeft',
            style: { fontSize: 11, fill: '#64748b', textAnchor: 'middle' },
          }}
        />
        <Tooltip content={<IndexTooltip />} />
        {/* The base period is 100 by construction; showing it stops the eye
            from reading a small move off a zoomed axis as a large one. */}
        <ReferenceLine
          y={100}
          stroke="#94a3b8"
          strokeDasharray="4 4"
          label={{ value: 'base = 100', position: 'right', fontSize: 10, fill: '#64748b' }}
        />
        {compare?.length > 0 && (
          <Line
            type="monotone"
            dataKey="compare"
            stroke="#94a3b8"
            strokeWidth={1.5}
            strokeDasharray="5 3"
            dot={false}
            isAnimationActive={false}
            name="comparison"
          />
        )}
        <Line
          type="monotone"
          dataKey="value"
          stroke="#0f172a"
          strokeWidth={2}
          dot={<ProvenanceDot />}
          activeDot={{ r: 6 }}
          isAnimationActive={false}
          name="index"
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

/**
 * The key for the dot colours, rendered next to every chart that uses them.
 *
 * It is a plain visible row, not a hover affordance: the whole point of the
 * per-dot encoding is that someone can read provenance off the chart in a
 * glance, and that fails if the key itself has to be discovered.
 */
export function ProvenanceLegend({
  hasCompare = false,
  compareLabel = 'other series',
  hasGrades = false,
}) {
  const items = [
    ['live', 'day includes fares scraped live'],
    ['live_limited', 'live, rate-capped source only'],
    ['simulated', 'day is fully simulated'],
    ['imputed', 'majority carried forward from an earlier day'],
  ]
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
      <span className="font-semibold uppercase tracking-wide text-slate-500">
        Each dot is one day — fill is its data source, ring is its confidence
      </span>
      {items.map(([kind, meaning]) => (
        <span key={kind} className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
            style={{ backgroundColor: PROVENANCE_COLOURS[kind] }}
          />
          {meaning}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <span
          className="inline-block h-3 w-3 shrink-0 rounded-full border-2"
          style={{ borderColor: PROVENANCE_COLOURS.surge, background: 'transparent' }}
        />
        red ring = too few routes covered that day
      </span>
      {hasGrades && (
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-3 w-3 shrink-0 rounded-full border-2 bg-white"
            style={{ borderColor: GRADE_COLOURS.INDICATIVE }}
          />
          ring colour = confidence grade (see panel below)
        </span>
      )}
      {hasCompare && (
        <span className="inline-flex items-center gap-1.5">
          <svg width="24" height="8" aria-hidden="true">
            <line
              x1="0"
              y1="4"
              x2="24"
              y2="4"
              stroke="#94a3b8"
              strokeWidth="1.5"
              strokeDasharray="5 3"
            />
          </svg>
          grey dashed = {compareLabel}
        </span>
      )}
    </div>
  )
}
