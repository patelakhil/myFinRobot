import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { AlertsModal, type AlertsModalProps } from './AlertsModal'
import type { Alert } from '../../api/portfolio'

function alert(over: Partial<Alert> & { id: string }): Alert {
  return {
    portfolio_id: 'pf_1',
    ticker: 'AAPL',
    kind: 'price_above',
    threshold: 250,
    window: null,
    repeat: false,
    enabled: true,
    note: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    last_evaluated_at: null,
    last_condition: null,
    last_value: null,
    last_triggered_at: null,
    trigger_count: 0,
    ...over,
  }
}

function renderModal(over: Partial<AlertsModalProps> = {}) {
  const props: AlertsModalProps = {
    ticker: 'AAPL',
    price: 200,
    currency: 'USD',
    alerts: [],
    loading: false,
    busy: false,
    error: null,
    evaluateBusy: false,
    evaluateReport: undefined,
    onCreate: vi.fn(),
    onToggle: vi.fn(),
    onDelete: vi.fn(),
    onEvaluate: vi.fn(),
    onClose: vi.fn(),
    ...over,
  }
  render(<AlertsModal {...props} />)
  return props
}

describe('AlertsModal', () => {
  it('describes existing rules and wires toggle / delete', () => {
    const props = renderModal({
      alerts: [
        alert({ id: 'a1', kind: 'price_below', threshold: 150, repeat: true }),
        alert({
          id: 'a2',
          kind: 'ma_cross_above',
          threshold: null,
          window: 200,
          enabled: false,
          trigger_count: 1,
          last_triggered_at: new Date().toISOString(),
        }),
        alert({ id: 'a3', kind: 'pnl_pct_below', threshold: -20 }),
      ],
    })
    const list = screen.getByTestId('alerts-list')
    expect(list).toHaveTextContent('Price below $150.00')
    expect(list).toHaveTextContent('repeats')
    expect(list).toHaveTextContent('Crosses above the 200-day moving average')
    expect(list).toHaveTextContent('fired')
    expect(list).toHaveTextContent('Position down to -20% or worse')

    const row = screen.getByTestId('alert-row-a2')
    fireEvent.click(within(row).getByRole('checkbox'))
    expect(props.onToggle).toHaveBeenCalledWith(expect.objectContaining({ id: 'a2' }), true)
    fireEvent.click(within(row).getByRole('button', { name: 'Delete alert' }))
    expect(props.onDelete).toHaveBeenCalledWith(expect.objectContaining({ id: 'a2' }))
  })

  it('switches fields by kind and submits the right payload', () => {
    const props = renderModal()
    const form = screen.getByTestId('alert-form')
    // price_above default: threshold pre-filled +10%.
    expect((within(form).getByLabelText('Price (USD)') as HTMLInputElement).value).toBe('220')

    fireEvent.change(within(form).getByLabelText('Rule'), { target: { value: 'ma_cross_below' } })
    expect(within(form).queryByLabelText('Price (USD)')).not.toBeInTheDocument()
    fireEvent.change(within(form).getByLabelText('Moving average'), { target: { value: '200' } })
    fireEvent.click(within(form).getByLabelText('Repeat (re-arm after it clears)'))
    fireEvent.click(screen.getByTestId('alert-add-btn'))
    expect(props.onCreate).toHaveBeenCalledWith({
      ticker: 'AAPL',
      kind: 'ma_cross_below',
      threshold: null,
      window: 200,
      repeat: true,
      note: null,
    })

    fireEvent.change(within(form).getByLabelText('Rule'), { target: { value: 'pnl_pct_below' } })
    expect((within(form).getByLabelText('Percent') as HTMLInputElement).value).toBe('-20')
    fireEvent.change(within(form).getByLabelText('Percent'), { target: { value: '15' } })
    expect(form).toHaveTextContent('Use a negative percent for a loss limit')
    expect(screen.getByTestId('alert-add-btn')).toBeDisabled()
  })

  it('"check now" calls evaluate and reports the outcome', () => {
    const props = renderModal({
      evaluateReport: {
        evaluated: 3,
        fired: 1,
        skipped: 0,
        tickers: 2,
        notifications: ['n'],
        errors: [],
      },
    })
    const btn = screen.getByTestId('alert-evaluate-btn')
    expect(btn).toHaveTextContent('Checked 3 rules · 1 fired')
    fireEvent.click(btn)
    expect(props.onEvaluate).toHaveBeenCalled()
  })
})
