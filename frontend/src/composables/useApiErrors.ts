import { useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'

import { isAbortError, toApiError } from '@/api'

/** Turn any thrown value from the API layer into a localized message (and optionally toast it). */
export function useApiErrors() {
  const { t } = useI18n()
  const message = useMessage()

  function describe(e: unknown): string {
    const err = toApiError(e)
    const withDetail = (key: string): string =>
      err.detail ? `${t(key)}：${err.detail}` : t(key)
    switch (err.status) {
      case 0:
        return t('errors.network')
      case 401:
        return t('errors.unauthorized')
      case 403:
        return withDetail('errors.forbidden')
      case 404:
        return withDetail('errors.notFound')
      case 409:
        return withDetail('errors.conflict')
      case 413:
        return withDetail('errors.tooLarge')
      case 415:
        return withDetail('errors.unsupported')
      case 422:
        return withDetail('errors.validation')
      case 501:
        return t('errors.notImplemented')
      default:
        if (err.status >= 500) return withDetail('errors.server')
        return err.detail ?? err.message
    }
  }

  /** Toast an error; aborts are ignored. */
  function notify(e: unknown, context?: string): void {
    if (isAbortError(e)) return
    const text = describe(e)
    message.error(context ? `${context}：${text}` : text, { duration: 6000, closable: true })
  }

  return { describe, notify }
}
