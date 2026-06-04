import { describe, expect, it } from 'vitest'

import type { CronJobRun } from '@/types/hermes'

import { buildCronHistoryMessages } from './history-dialog'

function run(overrides: Partial<CronJobRun> = {}): CronJobRun {
  return {
    ended_at: 1_715_000_065,
    end_reason: 'cron_complete',
    message_count: 4,
    messages: [
      { content: 'Generate the briefing', role: 'user', timestamp: 1_715_000_000 },
      {
        content: '',
        role: 'assistant',
        timestamp: 1_715_000_010,
        tool_calls: [{ function: { arguments: '{}', name: 'text_to_speech' }, id: 'call-1' }]
      },
      {
        content: '{"success":true,"path":"/tmp/briefing.mp3"}',
        role: 'tool',
        timestamp: 1_715_000_020,
        tool_call_id: 'call-1',
        tool_name: 'text_to_speech'
      },
      { content: 'MEDIA: /tmp/briefing.mp3', role: 'assistant', timestamp: 1_715_000_030 }
    ],
    session_id: 'cron_job-1_20240504_120000',
    started_at: 1_715_000_000,
    ...overrides
  }
}

describe('buildCronHistoryMessages', () => {
  it('adds visible run timing metadata and preserves media messages', () => {
    const messages = buildCronHistoryMessages([run()])

    expect(messages[0].role).toBe('system')
    expect(messages[0].parts[0]).toMatchObject({
      type: 'text',
      text: expect.stringContaining('Duration: 1m 5s')
    })

    const mediaPart = messages
      .flatMap(message => message.parts)
      .find(part => part.type === 'text' && part.text.includes('#media:%2Ftmp%2Fbriefing.mp3'))

    expect(mediaPart).toMatchObject({
      type: 'text',
      text: expect.stringContaining('#media:%2Ftmp%2Fbriefing.mp3')
    })
  })

  it('labels missing media instead of rendering an unusable player', () => {
    const messages = buildCronHistoryMessages([run({ unavailable_media: ['/tmp/briefing.mp3'] })])

    const text = messages
      .flatMap(message => message.parts)
      .filter(part => part.type === 'text')
      .map(part => part.text)
      .join('\n')

    expect(text).toContain('Audio: briefing.mp3 unavailable (file was not created or no longer exists)')

    expect(text).not.toContain('#media:%2Ftmp%2Fbriefing.mp3')
  })

  it('prefixes message and tool-call ids across runs', () => {
    const first = run()
    const second = run({ session_id: 'cron_job-1_20240505_120000' })
    const messages = buildCronHistoryMessages([second, first])
    const ids = messages.map(message => message.id)

    const toolIds = messages.flatMap(message =>
      message.parts.flatMap(part => (part.type === 'tool-call' ? [part.toolCallId] : []))
    )

    expect(ids[0]).toBe(`${first.session_id}:run-header`)
    expect(ids.at(-1)?.startsWith(second.session_id)).toBe(true)
    expect(new Set(ids).size).toBe(ids.length)
    expect(new Set(toolIds).size).toBe(toolIds.length)
    expect(ids.every(id => id.startsWith('cron_job-1_'))).toBe(true)
  })
})
