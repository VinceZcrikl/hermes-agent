import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getCronJobs = vi.fn()

vi.mock('@/hermes', () => ({
  createCronJob: vi.fn(),
  deleteCronJob: vi.fn(),
  getCronJobs: () => getCronJobs(),
  pauseCronJob: vi.fn(),
  resumeCronJob: vi.fn(),
  triggerCronJob: vi.fn(),
  updateCronJob: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('./history-dialog', () => ({
  CronHistoryDialog: ({ job }: { job: null | { id: string } }) =>
    job ? <div role="dialog">History for {job.id}</div> : null
}))

beforeEach(() => {
  getCronJobs.mockResolvedValue([
    {
      enabled: true,
      id: 'daily-briefing',
      name: 'Daily briefing',
      prompt: 'Summarize the day',
      schedule: { expr: '0 9 * * *' },
      state: 'scheduled'
    }
  ])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('CronView message preview', () => {
  it('keeps history inside the actions menu', async () => {
    const { CronView } = await import('./index')

    render(
      <MemoryRouter>
        <CronView onClose={() => {}} />
      </MemoryRouter>
    )

    const actions = await screen.findByRole('button', { name: 'Actions for Daily briefing' })

    expect(screen.queryByRole('button', { name: 'Preview messages for Daily briefing' })).toBeNull()

    actions.focus()
    fireEvent.keyDown(actions, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Message history' }))

    await waitFor(() => expect(screen.getByRole('dialog').textContent).toContain('daily-briefing'))
  })
})
