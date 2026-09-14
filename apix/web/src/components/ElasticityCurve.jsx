/**
 * Advance-purchase elasticity curve.
 *
 * Median fare by how far ahead the ticket is bought, indexed to T+30. This is
 * the chart that answers the policy question the CPI cannot: not just "did
 * fares rise" but "did the penalty for booking late rise", which is what a
 * traveller with an urgent journey actually experiences.
 *
 * Plotted right-to-left — 45 days out on the left, tomorrow on the right — so
 * the curve reads in the direction time runs.
 */
import React from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

function CurveTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  return (
    <div className="rounded-md border border-slate-300 bg-white p-3 text-xs shadow-lg">
      <div className="font-semibold">{p.window} before departure</div>
      <div className="mt-1 font-mono">
        median ₹{p.median.toLocaleString()} · mean ₹{p.mean.toLocaleString()}
      </div>
      <div className="mt-1 text-slate-600">
        {p.ratio === null
          ? 'No T+30 anchor in this window, so no ratio is shown.'
          : `${p.ratio.toFixed(2)}x the T+30 fare`}
      </div>
      <div className="text-slate-500">n = {p.n} quotes</div>
    </div>
  )
}

export default function ElasticityCurve({ curve, height = 360 }) {
  if (!curve?.points?.length) {
    return (
      <p className="py-8 text-center text-sm text-slate-500">
        No observations for this route in the window.
      </p>
    )
  }

  const data = [...curve.points]
    .sort((a, b) => b.days - a.days)
    .map((p) => ({
      window: p.advance_window,
      days: p.days,
      median: p.median_fare,
      mean: p.mean_fare,
      n: p.n,
      ratio: p.ratio_to_t30,
    }))

  const hasAnchor = data.some((d) => d.days === 30 && d.ratio !== null)

  return (
    <div className="space-y-3">
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ top: 24, right: 12, bottom: 12, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
          <XAxis
            dataKey="window"
            tick={{ fontSize: 12, fill: '#64748b' }}
            label={{
              value: 'days booked before departure (45 on the left, 1 on the right)',
              position: 'insideBottom',
              offset: -2,
              style: { fontSize: 11, fill: '#64748b' },
            }}
            height={44}
          />
          <YAxis
            tick={{ fontSize: 12, fill: '#64748b' }}
            width={72}
            tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`}
            label={{
              value: 'median fare',
              angle: -90,
              position: 'insideLeft',
              style: { fontSize: 11, fill: '#64748b', textAnchor: 'middle' },
            }}
          />
          <Tooltip content={<CurveTooltip />} cursor={{ fill: '#f1f5f9' }} />
          <Bar dataKey="median" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {data.map((d) => (
              <Cell
                key={d.window}
                // The anchor is drawn in a different colour so nobody reads
                // its 1.00x as a finding about T+30 pricing.
                fill={d.days === 30 ? '#0f172a' : '#475569'}
              />
            ))}
            <LabelList
              dataKey="ratio"
              position="top"
              fontSize={12}
              fill="#334155"
              formatter={(v) => (v === null ? '' : `${v.toFixed(2)}x`)}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      <div className="flex flex-wrap items-baseline justify-between gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-3 shrink-0 rounded-sm"
            style={{ backgroundColor: '#0f172a' }}
          />
          T+30 anchor (1.00x by definition)
          <span className="ml-3 inline-flex items-center gap-1.5">
            <span
              className="inline-block h-3 w-3 shrink-0 rounded-sm"
              style={{ backgroundColor: '#475569' }}
            />
            other windows · the <strong>Nx</strong> above each bar is its fare
            divided by the T+30 fare
          </span>
        </span>
        {curve.max_spread_pct !== null && curve.max_spread_pct !== undefined && (
          <span>
            Widest gap across windows:{' '}
            <strong className="font-mono">
              {curve.max_spread_pct.toFixed(1)}%
            </strong>
          </span>
        )}
      </div>
      {!hasAnchor && (
        <p className="text-xs text-amber-700">
          No T+30 observations in this window, so no ratios can be computed.
        </p>
      )}
    </div>
  )
}
