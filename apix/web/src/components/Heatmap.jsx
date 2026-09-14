/**
 * Route x advance-window heatmap.
 *
 * The grid shows movement against the base period, not absolute rupee levels.
 * Levels would mostly encode route distance — Delhi-Mumbai is cheaper than
 * Delhi-Bengaluru because it is shorter, which is not news and would swamp the
 * signal.
 *
 * An unmatched cell is rendered as hatched grey, never as 0%. A missing
 * comparison and an unchanged price look nothing alike here, because they are
 * nothing alike: one is an absence of information, the other is information.
 *
 * PER-CELL PROVENANCE
 * Each square carries a printed tag — LIVE / SIM / IMP — in its corner, not a
 * tooltip. This is the one surface in the whole dashboard where coverage is
 * legible cell by cell, so "which twenty of these thirty are real?" should be
 * answerable by looking, including from a screenshot. A cell backed by more
 * than one tier is tagged with the weakest tier present (the API decides that;
 * this file only renders it), so the grid never rounds a part-filled cell up.
 */
import React from 'react'

const WINDOWS = ['T+1', 'T+7', 'T+15', 'T+30', 'T+45']

/** How each provenance tier prints on a cell. Short, because it shares a 24px row. */
const TIER = {
  live: { tag: 'LIVE', cls: 'bg-live text-white', full: 'scraped live (airline direct)' },
  live_limited: {
    tag: 'LIVE*',
    cls: 'bg-liveLimited text-white',
    full: 'scraped live from a rate-capped source',
  },
  imputed: {
    tag: 'IMP',
    cls: 'bg-imputed text-white',
    full: 'carried forward from an earlier cycle',
  },
  simulated: {
    tag: 'SIM',
    cls: 'bg-simulated text-white',
    full: 'simulated fallback — not an observed fare',
  },
}

/** Diverging red/blue scale. Null returns null so callers must handle it. */
function cellColour(pct) {
  if (pct === null || pct === undefined) return null
  const capped = Math.max(-30, Math.min(30, pct))
  const intensity = Math.abs(capped) / 30
  const alpha = 0.12 + intensity * 0.68
  return capped >= 0
    ? `rgba(220, 38, 38, ${alpha})` // dearer
    : `rgba(37, 99, 235, ${alpha})` // cheaper
}

export default function Heatmap({ heatmap }) {
  if (!heatmap?.cells?.length) {
    return (
      <p className="py-8 text-center text-sm text-slate-500">
        No cells for this cycle.
      </p>
    )
  }

  const byRoute = new Map()
  for (const cell of heatmap.cells) {
    if (!byRoute.has(cell.route)) byRoute.set(cell.route, {})
    byRoute.get(cell.route)[cell.advance_window] = cell
  }
  const routes = [...byRoute.keys()].sort()
  const isBasePeriod = heatmap.cycle_date === heatmap.base_period

  // Counted from what is actually on screen, so the sentence under the grid and
  // the squares above it cannot disagree.
  const totalCells = routes.length * WINDOWS.length
  const liveCells = heatmap.cells.filter((c) => c.is_live).length
  const liveRoutes = routes.filter((r) =>
    WINDOWS.some((w) => byRoute.get(r)[w]?.is_live),
  )

  return (
    <div className="space-y-4">
      {isBasePeriod && (
        <p className="caveat border-slate-400 bg-slate-50 text-slate-700">
          This cycle <em>is</em> the base period, so every cell reads 0.0% by
          construction. That is arithmetic, not a finding.
        </p>
      )}

      <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
        <strong className="font-semibold">
          {liveCells} of {totalCells} cells
        </strong>{' '}
        in this grid are real fares scraped live (
        {totalCells ? ((liveCells / totalCells) * 100).toFixed(0) : 0}%), across{' '}
        <strong className="font-semibold">
          {liveRoutes.length} of {routes.length} routes
        </strong>
        . The rest are labelled simulated fallback and tagged{' '}
        <span className="rounded bg-simulated px-1 font-mono text-[10px] text-white">
          SIM
        </span>{' '}
        on the square itself.
      </p>

      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-1.5 text-sm">
          <thead>
            <tr>
              <th className="px-2 py-1 text-left text-xs font-medium text-slate-500">
                Route
              </th>
              {WINDOWS.map((w) => (
                <th
                  key={w}
                  className="px-2 py-1 text-center text-xs font-medium text-slate-500"
                >
                  {w}
                  <span className="block text-[10px] font-normal text-slate-400">
                    {w.slice(2)} days ahead
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {routes.map((route) => (
              <tr key={route}>
                <td className="whitespace-nowrap px-2 py-1 font-mono text-sm font-medium">
                  {route}
                </td>
                {WINDOWS.map((w) => {
                  const cell = byRoute.get(route)[w]
                  if (!cell) {
                    return (
                      <td key={w} className="p-0">
                        <div
                          className="flex h-16 w-28 items-center justify-center rounded border border-dashed border-slate-300 text-[11px] text-slate-400"
                          title="No observation in this cell for this cycle"
                        >
                          no data
                        </div>
                      </td>
                    )
                  }

                  const pct = cell.pct_change_vs_base
                  const unmatched = pct === null || pct === undefined
                  const tier = TIER[cell.source_type] || TIER.simulated
                  return (
                    <td key={w} className="p-0">
                      <div
                        className={`relative flex h-16 w-28 flex-col items-center justify-center rounded border ${
                          unmatched
                            ? 'border-dashed border-slate-400 bg-slate-100 text-slate-500'
                            : 'border-transparent'
                        }`}
                        style={
                          unmatched ? undefined : { backgroundColor: cellColour(pct) }
                        }
                        title={
                          unmatched
                            ? `${route} ${w}: median ₹${cell.median_fare.toLocaleString()} from ${cell.n} quotes, ${tier.full}. No matching cell in the base period, so no comparison exists.`
                            : `${route} ${w}: median ₹${cell.median_fare.toLocaleString()} from ${cell.n} quotes, ${tier.full}, ${pct >= 0 ? '+' : ''}${pct.toFixed(1)}% vs base`
                        }
                      >
                        {/* Printed, not hovered. A screenshot of this grid has
                            to answer "is this real?" on its own. */}
                        <span
                          className={`absolute left-1 top-1 rounded px-1 font-mono text-[9px] font-semibold leading-tight ${tier.cls}`}
                        >
                          {tier.tag}
                        </span>
                        {unmatched ? (
                          <span className="mt-2 text-[11px] italic">unmatched</span>
                        ) : (
                          <>
                            <span className="mt-2 font-mono text-sm font-semibold tabular-nums">
                              {pct >= 0 ? '+' : ''}
                              {pct.toFixed(1)}%
                            </span>
                            <span className="font-mono text-[10px] opacity-70">
                              ₹{Math.round(cell.median_fare).toLocaleString()} · n=
                              {cell.n}
                            </span>
                          </>
                        )}
                      </div>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
        <span className="font-semibold uppercase tracking-wide text-slate-500">
          Key
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-6 rounded"
            style={{ backgroundColor: 'rgba(37, 99, 235, 0.7)' }}
          />
          cheaper than base
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-6 rounded"
            style={{ backgroundColor: 'rgba(220, 38, 38, 0.7)' }}
          />
          dearer than base
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-3 w-6 rounded border border-dashed border-slate-400 bg-slate-100" />
          no comparison available — <em>not</em> 0%
        </span>
        {Object.entries(TIER).map(([kind, t]) => (
          <span key={kind} className="inline-flex items-center gap-1.5">
            <span
              className={`inline-block rounded px-1 font-mono text-[9px] font-semibold ${t.cls}`}
            >
              {t.tag}
            </span>
            {t.full}
          </span>
        ))}
      </div>
    </div>
  )
}
