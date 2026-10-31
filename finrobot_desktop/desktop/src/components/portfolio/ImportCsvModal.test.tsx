import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { ImportCsvModal, type ImportCsvModalProps } from './ImportCsvModal'
import type { ImportPreview } from '../../api/portfolio'

const PREVIEW: ImportPreview = {
  rows: [
    {
      line_no: 2,
      ticker: 'AAPL',
      side: 'buy',
      units: 10,
      cost_per_unit: 150.25,
      purchase_date: '2024-01-15',
      currency: 'USD',
      note: null,
      errors: [],
    },
    {
      line_no: 3,
      ticker: null,
      side: 'buy',
      units: 3,
      cost_per_unit: 10,
      purchase_date: '2024-01-01',
      currency: 'USD',
      note: null,
      errors: ["Invalid ticker 'bad ticker!'"],
    },
  ],
  valid_count: 1,
  error_count: 1,
  columns_detected: { ticker: 'Symbol', units: 'Qty' },
  delimiter: ',',
  imported: null,
  portfolio_id: 'pf_1',
}

function renderModal(over: Partial<ImportCsvModalProps> = {}) {
  const props: ImportCsvModalProps = {
    portfolioId: 'pf_1',
    defaultCurrency: 'USD',
    preview: undefined,
    previewBusy: false,
    previewError: null,
    commitBusy: false,
    commitError: null,
    onPreview: vi.fn(),
    onCommit: vi.fn(),
    onCancel: vi.fn(),
    ...over,
  }
  render(<ImportCsvModal {...props} />)
  return props
}

describe('ImportCsvModal', () => {
  it('disables preview until there is input, then sends the pasted CSV', () => {
    const props = renderModal()
    const btn = screen.getByTestId('import-preview-btn')
    expect(btn).toBeDisabled()
    fireEvent.change(screen.getByTestId('import-textarea'), {
      target: { value: 'ticker,units,cost,date\nAAPL,1,2,2024-01-01' },
    })
    expect(btn).not.toBeDisabled()
    fireEvent.click(btn)
    expect(props.onPreview).toHaveBeenCalledWith(
      'ticker,units,cost,date\nAAPL,1,2,2024-01-01',
      'USD',
    )
    // Nothing to commit before a preview exists.
    expect(screen.getByTestId('import-commit-btn')).toBeDisabled()
  })

  it('renders the per-row preview with reasons and commits only when rows are valid', () => {
    const props = renderModal({ preview: PREVIEW })
    fireEvent.change(screen.getByTestId('import-textarea'), { target: { value: 'x' } })
    const preview = screen.getByTestId('import-preview')
    expect(within(preview).getByText('1 rows ready to import')).toBeInTheDocument()
    expect(within(preview).getByText('1 rows will be skipped')).toBeInTheDocument()
    expect(within(screen.getByTestId('import-row-2')).getByText('OK')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('import-row-3')).getByText("Invalid ticker 'bad ticker!'"),
    ).toBeInTheDocument()

    const commit = screen.getByTestId('import-commit-btn')
    expect(commit).toHaveTextContent('Import 1 rows')
    fireEvent.click(commit)
    expect(props.onCommit).toHaveBeenCalledWith('x', 'USD', null)
  })

  it('keeps commit disabled when every row is rejected', () => {
    renderModal({ preview: { ...PREVIEW, valid_count: 0, error_count: 2 } })
    fireEvent.change(screen.getByTestId('import-textarea'), { target: { value: 'x' } })
    expect(screen.getByTestId('import-commit-btn')).toBeDisabled()
  })

  it('offers a portfolio name on the first-run path and passes it to commit', () => {
    const props = renderModal({ portfolioId: null, preview: PREVIEW })
    fireEvent.change(screen.getByTestId('import-textarea'), { target: { value: 'x' } })
    fireEvent.change(screen.getByPlaceholderText('My Portfolio'), { target: { value: 'Book A' } })
    fireEvent.click(screen.getByTestId('import-commit-btn'))
    expect(props.onCommit).toHaveBeenCalledWith('x', 'USD', 'Book A')
  })
})
