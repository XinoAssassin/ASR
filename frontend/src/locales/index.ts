import { createI18n } from 'vue-i18n'

import en from './en'
import zhCN, { type MessageSchema } from './zh-CN'

export type AppLocale = 'zh-CN' | 'en'

export const LOCALE_STORAGE_KEY = 'asr.locale'

function initialLocale(): AppLocale {
  try {
    return window.localStorage.getItem(LOCALE_STORAGE_KEY) === 'en' ? 'en' : 'zh-CN'
  } catch {
    return 'zh-CN'
  }
}

export const i18n = createI18n<[MessageSchema], AppLocale>({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: 'zh-CN',
  messages: { 'zh-CN': zhCN, en },
})
