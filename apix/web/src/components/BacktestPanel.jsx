/**
 * Backtest against DGCA monthly averages.
 *
 * WHY THE CAVEAT IS INSIDE THE BOX
 * When `reportable` is false the numbers are still shown, but wrapped — struck
 * through, greyed, and stamped — so that a cropped screenshot carries the mark
 * with it. Hiding the result outright would invite someone to recompute it by
 * hand and quote it with no caveat at all; showing it plainly would be worse.
 *
 * `reportable` is decided by the API, not here. One definition, read by the
 * CLI, the API and this page alike.
 *
 * DGCA data is the yardstick and never an index input. That separation is
 * enforced by tests (tests/test_dgca_separation.py), not just asserted here.
 */
import React from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

function Metric({ label, value, hint, muted }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div
        className={`stat ${muted ? 'text-slate-400 line-through decoration-red-400 decoration-2' : ''}`}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  )
}

export default function BacktestPanel({ backtest }) {
  if (!backtest) return null

  const {
    months_compared: months,
    pearson_r: r,
    mape,
    reportable,
    uses_placeholder_reference: placeholder,
    notes = [],
    pairs = [],
  } = backtest

  const fmt = (v, digits = 3) =>
    v === null || v === undefined ? '—' : v.toFixed(digits)

  return (
    <div className="space-y-5">
      {!reportable && (
        <div className="caveat border-red-500 bg-red-50 text-red-900">
          <div className="font-semibold uppercase tracking-wide">
            Not reportable — do not cite these figures
          </div>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
          {placeholder && (
            <p className="mt-2">
              The reference rows are illustrative placeholders, not published
              DGCA figures. Replace them with the real monthly series (
              <code className="font-mono text-xs">apix seed-dgca</code>) before
              this section means anything.
            </p>
          )}
        </div>
      )}

      {reportable && notes.length > 0 && (
        <div className="caveat border-slate-400 bg-slate-50 text-slate-700">
          <ul className="list-disc space-y-1 pl-5">
            {notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
        <Metric
          label="Months compared"
          value={months ?? 0}
          hint={months < 6 ? 'below the threshold for a meaningful r' : null}
        />
        <Metric
          label="Pearson r"
          value={fmt(r)}
          muted={!reportable}
          hint={r === null ? 'undefined — no variance or no overlap' : null}
        />
        <Metric label="MAPE" value={mape === null ? '—' : `${fmt(mape, 2)}%`} muted={!reportable} />
        <Metric
          label="Reportable"
          value={reportable ? 'yes' : 'no'}
          hint="one flag, read by the CLI, the API and this page"
        />
      </div>

      {pairs.length > 0 ? (
        <div>
          <p className="mb-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
            Both series are rebased to 100 at the first month they share, so the
            lines are comparable in shape rather than in rupees. Solid dark =
            APIx (this system); grey dashed = DGCA&apos;s published monthly
            average. They are two independent measurements of the same market,
            not one derived from the other.
          </p>
          <ResponsiveContainer width="100%" height={340}>
            <LineChart
              data={pairs.map((p) => ({
                month: p.month,
                APIx: p.apix,
                DGCA: p.dgca,
                error: p.abs_pct_error,
                routes: p.routes_used,
              }))}
              margin={{ top: 8, right: 16, bottom: 4, left: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="month" tick={{ fontSize: 12, fill: '#64748b' }} />
              <YAxis
                tick={{ fontSize: 12, fill: '#64748b' }}
                width={68}
                label={{
                  value: 'rebased to 100',
                  angle: -90,
                  position: 'insideLeft',
                  style: { fontSize: 11, fill: '#64748b', textAnchor: 'middle' },
                }}
              />
              <Tooltip
                formatter={(v, name) => [
                  typeof v === 'number' ? v.toFixed(2) : v,
                  name,
                ]}
              />
              <Legend wrapperStyle={{ fontSize: 13 }} />
              <Line
                type="monotone"
                dataKey="APIx"
                stroke="#0f172a"
                strokeWidth={2}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="DGCA"
                stroke="#94a3b8"
                strokeWidth={2}
                strokeDasharray="5 3"
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
          {!reportable && (
            <p className="mt-1 text-center text-xs font-semibold uppercase tracking-widest text-red-600">
              illustrative only — not a validation result
            </p>
          )}
        </div>
      ) : (
        <p className="py-6 text-center text-sm text-slate-500">
          No overlapping months between the index and the reference series.
          Nothing is plotted, and no correlation is implied by that absence.
        </p>
      )}
    </div>
  )
}
