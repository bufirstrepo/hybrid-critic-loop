// The Millionaire Matrix — Native Hermes Desktop Plugin (uncompiled ESM)
import {
  Badge,
  Button,
  EmptyState,
  GlyphSpinner,
  haptic,
  host,
  PALETTE_AREA,
  PANES_AREA,
  queryClient,
  ScrollArea,
  Separator,
  STATUSBAR_AREAS,
  StatusDot
  ,
  useQuery
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'the-millionaire-matrix'
const PANE_FULL_ID = `${PLUGIN_ID}:pane`

function phaseMeta(activeRun, readyCount, totalCount) {
  const active = Boolean(activeRun?.active)
  const phase = String(activeRun?.phase || 'idle')
  if (active) {
    const round = activeRun?.bracket?.current_round
    const detail = phase === 'matrix' && round ? `R${round} MATRIX` : phase.toUpperCase()
    return {
      active: true,
      tone: 'good',
      chipLabel: `MM · RUNNING · ${detail}`,
      badgeLabel: `RUNNING · ${detail}`
    }
  }
  if (phase === 'awaiting_human_gate') {
    return {
      active: false,
      tone: 'warn',
      chipLabel: 'MM · GATE PENDING',
      badgeLabel: 'HUMAN GATE PENDING'
    }
  }
  if (phase === 'completed') {
    return {
      active: false,
      tone: 'good',
      chipLabel: activeRun?.certificate_stamp ? 'MM · CERTIFIED' : 'MM · COMPLETED',
      badgeLabel: activeRun?.certificate_stamp ? 'MM CERTIFIED' : 'COMPLETED'
    }
  }
  if (phase === 'escalated' || phase === 'error' || phase === 'refused_baseline') {
    return {
      active: false,
      tone: 'bad',
      chipLabel: `MM · ${phase.toUpperCase()}`,
      badgeLabel: phase.toUpperCase()
    }
  }
  const panelTag = totalCount > 0 ? `${readyCount}/${totalCount} READY` : 'IDLE'
  return {
    active: false,
    tone: readyCount >= 4 ? 'good' : 'muted',
    chipLabel: `MM · ${panelTag}`,
    badgeLabel: 'IDLE'
  }
}

function StatusChip({ ctx }) {
  const { data } = useQuery({
    queryKey: [ctx.source, 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: query => (query.state.data?.active_run?.active ? 1500 : 8000),
    retry: false
  })

  const activeRun = data?.active_run
  const readyCount = data?.panel?.ready_count ?? 0
  const totalCount = data?.panel?.total_count ?? 0
  const meta = phaseMeta(activeRun, readyCount, totalCount)

  return jsxs('button', {
    type: 'button',
    title: activeRun?.message || 'Open The Millionaire Matrix pane',
    className:
      'inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[0.6875rem] text-(--ui-text-secondary) hover:text-(--ui-text-primary)',
    onClick: () => {
      haptic('tap')
      host.revealPane?.(PANE_FULL_ID)
    },
    children: [
      meta.active
        ? jsx(GlyphSpinner, { className: 'size-3 text-(--ui-accent)' })
        : jsx(StatusDot, { tone: meta.tone }),
      jsx('span', { className: 'font-mono tracking-tight', children: meta.chipLabel })
    ]
  })
}

function SeatRow({ seat }) {
  const status = String(seat.status || (seat.ready ? 'ready' : 'unavailable'))
  const eliminated = seat.eliminated_at
  const score = seat.score
  let tone = 'muted'
  if (status === 'running' || status === 'completed' || status === 'ready') {
    tone = eliminated ? 'warn' : 'good'
  } else if (status === 'error' || status === 'timeout' || status === 'refused') {
    tone = 'bad'
  }

  return jsxs('div', {
    className:
      'flex items-center justify-between gap-2 rounded border border-(--ui-stroke-secondary) px-2 py-1.5 text-xs',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-2',
        children: [
          status === 'running'
            ? jsx(GlyphSpinner, { className: 'size-3 shrink-0 text-(--ui-accent)' })
            : jsx(StatusDot, { tone, className: 'shrink-0' }),
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-1.5 truncate font-medium text-(--ui-text-primary)',
                children: [
                  jsx('span', { children: seat.expert_id }),
                  seat.family
                    ? jsx('span', {
                        className: 'font-mono text-[0.65rem] text-(--ui-text-tertiary)',
                        children: `[${seat.family}]`
                      })
                    : null
                ]
              }),
              seat.model && seat.model !== seat.expert_id
                ? jsx('div', {
                    className: 'truncate font-mono text-[0.65rem] text-(--ui-text-tertiary)',
                    children: seat.model
                  })
                : null
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-1.5 font-mono text-[0.68rem]',
        children: [
          score !== null && score !== undefined
            ? jsx(Badge, { variant: 'outline', children: `${Number(score).toFixed(1)}` })
            : null,
          jsx('span', {
            className: 'text-(--ui-text-secondary)',
            children: eliminated ? `out:${eliminated}` : status
          })
        ]
      })
    ]
  })
}

function MatrixCard({ matrix }) {
  if (!matrix || matrix.note === 'not run' || !matrix.candidates) {
    return null
  }
  const entries = Object.entries(matrix.candidates)
  if (entries.length === 0) {
    return null
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded border border-(--ui-stroke-secondary) p-2.5 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('span', {
            className: 'font-medium text-(--ui-text-primary)',
            children: 'Peer Validity Matrix'
          }),
          jsx('span', {
            className: 'font-mono text-[0.68rem] text-(--ui-text-tertiary)',
            children: `panel: ${matrix.panel_status || 'full'}`
          })
        ]
      }),
      jsx('div', {
        className: 'flex flex-col gap-1',
        children: entries.map(([cid, cand]) => {
          const judgeCount = Object.keys(cand.cells || {}).length
          const rowVals = (matrix.rows || [])
            .map(r => {
              const v = cand.row_results?.[r]?.median
              return v !== null && v !== undefined ? `${r.slice(0, 4)}:${Number(v).toFixed(1)}` : null
            })
            .filter(Boolean)
            .join(' ')
          return jsxs(
            'div',
            {
              className:
                'flex flex-col gap-0.5 rounded border border-(--ui-stroke-secondary) px-2 py-1',
              children: [
                jsxs('div', {
                  className: 'flex items-center justify-between font-mono text-[0.68rem]',
                  children: [
                    jsx('span', {
                      className: 'text-(--ui-text-primary)',
                      children: `${cand.expert_id} (${cand.family})`
                    }),
                    jsx('span', {
                      className: 'text-(--ui-text-tertiary)',
                      children: `${judgeCount} peer judges`
                    })
                  ]
                }),
                rowVals
                  ? jsx('div', {
                      className: 'font-mono text-[0.65rem] text-(--ui-text-secondary)',
                      children: rowVals
                    })
                  : null
              ]
            },
            cid
          )
        })
      })
    ]
  })
}

function MatrixPane({ ctx }) {
  const [refreshing, setRefreshing] = useState(false)
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: [ctx.source, 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: query => (query.state.data?.active_run?.active ? 1500 : 6000),
    retry: false
  })

  const activeRun = data?.active_run || {}
  const panelSeats = data?.panel?.seats || []
  const readyCount = data?.panel?.ready_count ?? 0
  const totalCount = data?.panel?.total_count ?? 0
  const meta = phaseMeta(activeRun, readyCount, totalCount)

  const runSeatsObj = activeRun?.seats || {}
  const runSeats = Object.values(runSeatsObj)
  const seatsToRender = runSeats.length > 0 ? runSeats : panelSeats
  const ladder = activeRun?.bracket?.ladder || []
  const lessons = data?.lessons || []

  const onRefresh = async () => {
    setRefreshing(true)
    try {
      await ctx.rest('/status?refresh_panel=true')
      await refetch()
    } catch {
      // Handled by query state
    } finally {
      setRefreshing(false)
    }
  }

  if (isLoading && !data) {
    return jsx('div', {
      className: 'flex h-full items-center justify-center p-4',
      children: jsx(GlyphSpinner, { className: 'size-4 text-(--ui-text-tertiary)' })
    })
  }

  if (error && !data) {
    return jsx('div', {
      className: 'flex h-full flex-col justify-center p-4',
      children: jsx(EmptyState, {
        title: 'Millionaire Matrix backend offline',
        description:
          'Enable the-millionaire-matrix in config.yaml (plugins.enabled) and restart the gateway to mount /api/plugins/the-millionaire-matrix.'
      })
    })
  }

  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsxs('div', {
      className: 'flex flex-col gap-3 p-3 text-sm',
      children: [
        jsxs('div', {
          className: 'flex items-center justify-between gap-2',
          children: [
            jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                meta.active
                  ? jsx(GlyphSpinner, { className: 'size-3.5 text-(--ui-accent)' })
                  : jsx(StatusDot, { tone: meta.tone }),
                jsx('span', {
                  className: 'font-semibold text-(--ui-text-primary)',
                  children: 'Millionaire Matrix'
                })
              ]
            }),
            jsxs('div', {
              className: 'flex items-center gap-1.5',
              children: [
                jsx(Badge, { variant: 'outline', children: meta.badgeLabel }),
                jsx(Button, {
                  size: 'sm',
                  variant: 'ghost',
                  disabled: refreshing,
                  onClick: onRefresh,
                  children: refreshing ? '…' : 'Refresh'
                })
              ]
            })
          ]
        }),

        jsxs('div', {
          className:
            'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) p-2.5 text-xs',
          children: [
            jsx('div', {
              className: 'text-(--ui-text-secondary)',
              children:
                activeRun?.message ||
                'Idle — ask Hermes to improve or fix code using Millionaire Matrix.'
            }),
            activeRun?.contract_id
              ? jsx('div', {
                  className: 'font-mono text-[0.68rem] text-(--ui-text-tertiary)',
                  children: `${activeRun.contract_id} · mode=${activeRun.mode || 'fix'} · ${activeRun.elapsed_s ?? 0}s`
                })
              : null,
            ladder.length > 0
              ? jsx('div', {
                  className: 'pt-1 font-mono text-xs text-(--ui-accent)',
                  children: `Bracket: ${ladder.join(' → ')}`
                })
              : null,
            activeRun?.winner
              ? jsx('div', {
                  className: 'pt-0.5 font-mono text-xs text-(--ui-text-primary)',
                  children: `Winner: ${activeRun.winner.expert_id} (${activeRun.winner.family})`
                })
              : null,
            activeRun?.certificate_stamp
              ? jsx('div', {
                  className: 'pt-0.5 font-mono text-[0.68rem] text-(--ui-text-secondary)',
                  children: activeRun.certificate_stamp
                })
              : null
          ]
        }),

        jsx(Separator, {}),

        jsxs('div', {
          className: 'flex flex-col gap-1.5',
          children: [
            jsxs('div', {
              className: 'flex items-center justify-between text-xs',
              children: [
                jsx('span', {
                  className: 'font-medium text-(--ui-text-secondary)',
                  children: runSeats.length > 0 ? 'Contest Seats (8 → 1)' : '8-Family Panel Readiness'
                }),
                jsx('span', {
                  className: 'font-mono text-[0.68rem] text-(--ui-text-tertiary)',
                  children: `${readyCount}/${totalCount} ready`
                })
              ]
            }),
            seatsToRender.length > 0
              ? jsx('div', {
                  className: 'flex flex-col gap-1',
                  children: seatsToRender.map(seat => jsx(SeatRow, { seat }, seat.expert_id))
                })
              : jsx('div', {
                  className: 'text-xs text-(--ui-text-tertiary)',
                  children: 'No seats reported yet.'
                })
          ]
        }),

        jsx(MatrixCard, { matrix: data?.latest_matrix }),

        data?.standing_line
          ? jsxs('div', {
              className:
                'rounded border border-(--ui-stroke-secondary) p-2 font-mono text-[0.68rem] text-(--ui-text-secondary)',
              children: ['Standing: ', data.standing_line]
            })
          : null,

        lessons.length > 0
          ? jsxs('div', {
              className: 'flex flex-col gap-1 text-xs',
              children: [
                jsx('span', {
                  className: 'font-medium text-(--ui-text-secondary)',
                  children: 'Recent Verified Lessons'
                }),
                ...lessons.slice(0, 3).map(lsn =>
                  jsx(
                    'div',
                    {
                      className:
                        'rounded border border-(--ui-stroke-secondary) px-2 py-1 text-[0.7rem] text-(--ui-text-tertiary)',
                      children: lsn.rule || lsn.outcome
                    },
                    lsn.id
                  )
                )
              ]
            })
          : null
      ]
    })
  })
}

export default {
  id: PLUGIN_ID,
  name: 'The Millionaire Matrix',
  register(ctx) {
    ctx.onEvent('plugin.the-millionaire-matrix.run.progress', () => {
      queryClient.invalidateQueries({ queryKey: [ctx.source, 'status'] })
    })

    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Millionaire Matrix',
        data: { placement: 'right', width: '340px' },
        render: () => jsx(MatrixPane, { ctx })
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 125,
        render: () => jsx(StatusChip, { ctx })
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'the-millionaire-matrix.open',
          label: 'Open Millionaire Matrix',
          keywords: ['millionaire', 'matrix', 'critic', 'panel', '8x1'],
          run: () => host.revealPane?.(PANE_FULL_ID)
        }
      }
    ])
  }
}
