import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { useEffect, useMemo, useState } from 'react'

import { Thread } from '@/components/assistant-ui/thread'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { type CronJob, type CronJobRun, getCronJobRuns } from '@/hermes'
import { type ChatMessage, type ChatMessagePart, textPart, toChatMessages } from '@/lib/chat-messages'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import { mediaDisplayLabel, mediaMarkdownHref } from '@/lib/media'

interface CronHistoryDialogProps {
  job: CronJob | null
  onClose: () => void
}

const RUN_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'medium'
})

function formatRunTime(value?: null | number): string {
  if (!value) {
    return '—'
  }

  const date = new Date(value * 1000)

  return Number.isNaN(date.valueOf()) ? '—' : RUN_TIME_FORMAT.format(date)
}

function formatDuration(startedAt: number, endedAt?: null | number): string {
  if (!startedAt || !endedAt || endedAt < startedAt) {
    return '—'
  }

  const seconds = Math.max(0, Math.round(endedAt - startedAt))
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60

  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`
}

function prefixToolCallIds(parts: ChatMessagePart[], sessionId: string): ChatMessagePart[] {
  return parts.map((part, index) =>
    part.type === 'tool-call'
      ? {
          ...part,
          toolCallId: `${sessionId}:${part.toolCallId || `${part.toolName}-${index}`}`
        }
      : part
  )
}

function markUnavailableMedia(parts: ChatMessagePart[], paths: string[]): ChatMessagePart[] {
  if (paths.length === 0) {
    return parts
  }

  return parts.map(part => {
    if (part.type !== 'text') {
      return part
    }

    const text = paths.reduce((current, path) => {
      const mediaLink = `[${mediaDisplayLabel(path)}](${mediaMarkdownHref(path)})`
      const unavailable = `${mediaDisplayLabel(path)} unavailable (file was not created or no longer exists)`

      return current.replaceAll(mediaLink, unavailable)
    }, part.text)

    return text === part.text ? part : { ...part, text }
  })
}

export function buildCronHistoryMessages(runs: CronJobRun[]): ChatMessage[] {
  return [...runs].reverse().flatMap((run, index) => {
    const runNumber = index + 1

    const header = [
      `Run ${runNumber} of ${runs.length}`,
      `Started: ${formatRunTime(run.started_at)}`,
      `Ended: ${formatRunTime(run.ended_at)}`,
      `Duration: ${formatDuration(run.started_at, run.ended_at)}`,
      `Messages: ${run.message_count}`,
      run.end_reason ? `Result: ${run.end_reason}` : ''
    ]
      .filter(Boolean)
      .join('\n')

    const messages = toChatMessages(run.messages).map(message => ({
      ...message,
      id: `${run.session_id}:${message.id}`,
      parts: markUnavailableMedia(prefixToolCallIds(message.parts, run.session_id), run.unavailable_media ?? [])
    }))

    return [
      {
        id: `${run.session_id}:run-header`,
        parts: [textPart(header)],
        role: 'system' as const,
        timestamp: run.started_at
      },
      ...messages
    ]
  })
}

function CronTranscript({ runs }: { runs: CronJobRun[] }) {
  const messages = useMemo<ThreadMessage[]>(() => buildCronHistoryMessages(runs).map(toRuntimeMessage), [runs])

  const runtime = useExternalStoreRuntime<ThreadMessage>({
    isRunning: false,
    messages,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread sessionKey={runs.map(run => run.session_id).join(':')} />
    </AssistantRuntimeProvider>
  )
}

export function CronHistoryDialog({ job, onClose }: CronHistoryDialogProps) {
  const [runs, setRuns] = useState<CronJobRun[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!job) {
      setRuns([])
      setLoading(false)
      setError('')

      return
    }

    let cancelled = false

    setRuns([])
    setLoading(true)
    setError('')

    void getCronJobRuns(job.id, job.profile_name || job.profile)
      .then(result => {
        if (!cancelled) {
          setRuns(result.runs)
        }
      })
      .catch(err => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load cron messages')
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [job])

  const messageCount = runs.reduce((total, run) => total + run.message_count, 0)

  return (
    <Dialog onOpenChange={open => !open && onClose()} open={job !== null}>
      <DialogContent className="h-[min(88vh,54rem)] max-h-[88vh] max-w-5xl grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-border/45 px-5 py-4 pr-12">
          <DialogTitle>{job ? `${job.name || job.id} messages` : 'Cron messages'}</DialogTitle>
          <DialogDescription>
            {loading
              ? 'Loading complete execution history...'
              : `${runs.length} run${runs.length === 1 ? '' : 's'} · ${messageCount} persisted message${
                  messageCount === 1 ? '' : 's'
                } · includes tool calls and media`}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 overflow-hidden bg-(--ui-chat-surface-background)">
          {loading ? (
            <div className="grid h-full place-items-center text-xs text-muted-foreground">Loading cron messages...</div>
          ) : error ? (
            <div className="grid h-full place-items-center px-6 text-center text-xs text-destructive">{error}</div>
          ) : runs.length === 0 ? (
            <div className="grid h-full place-items-center px-6 text-center">
              <div className="max-w-sm space-y-1.5">
                <div className="text-sm font-medium">No persisted messages yet</div>
                <p className="text-xs text-muted-foreground">
                  Messages appear here after this cron job completes an agent-backed run.
                </p>
              </div>
            </div>
          ) : (
            <div className="cron-history-transcript h-full [--composer-width:100%] [--titlebar-height:0px] [&_[data-role=user]]:static">
              <CronTranscript runs={runs} />
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
