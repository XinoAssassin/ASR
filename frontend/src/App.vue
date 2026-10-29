<script setup lang="ts">
import {
  type MenuOption,
  NAlert,
  NButton,
  NConfigProvider,
  NDialogProvider,
  NGlobalStyle,
  NLayout,
  NLayoutContent,
  NLayoutHeader,
  NMenu,
  NMessageProvider,
  NTag,
  NTooltip,
  darkTheme,
  dateEnUS,
  dateZhCN,
  enUS,
  useOsTheme,
  zhCN,
} from 'naive-ui'
import { computed, h, onBeforeUnmount, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import { getBackendUrl } from '@/api'
import { LOCALE_STORAGE_KEY, type AppLocale } from '@/locales'
import { isDesktop } from '@/platform'
import { useHealthStore } from '@/stores/health'
import { useRecorderStore } from '@/stores/recorder'

const { t, locale } = useI18n()
const route = useRoute()
const health = useHealthStore()
const osTheme = useOsTheme()
const desktop = isDesktop()
const recorder = useRecorderStore()

const theme = computed(() => (osTheme.value === 'dark' ? darkTheme : null))
const naiveLocale = computed(() => (locale.value === 'en' ? enUS : zhCN))
const naiveDateLocale = computed(() => (locale.value === 'en' ? dateEnUS : dateZhCN))

const navItems = [
  { key: 'upload', to: '/upload', label: 'nav.upload' },
  // Desktop only (the web build has no recorder).
  ...(desktop ? [{ key: 'recorder', to: '/recorder', label: 'nav.recorder' }] : []),
  { key: 'jobs', to: '/jobs', label: 'nav.jobs' },
  { key: 'settings', to: '/settings', label: 'nav.settings' },
] as const

const menuOptions = computed<MenuOption[]>(() =>
  navItems.map((item) => ({
    key: item.key,
    label: () =>
      h(RouterLink, { to: item.to, 'data-testid': `nav-${item.key}` }, () => [
        t(item.label),
        // Recording continues on other pages (and via the Ctrl+Alt+R hotkey): show it in the nav.
        item.key === 'recorder' && recorder.recording
          ? h('span', { class: 'nav-rec-dot', 'data-testid': 'nav-recording' })
          : null,
      ]),
  })),
)

const activeKey = computed(() => {
  const name = String(route.name ?? '')
  return name === 'transcript' ? 'jobs' : name
})

const healthTag = computed(() => {
  switch (health.state) {
    case 'connected':
      return { type: 'success' as const, text: t('health.connected') }
    case 'error':
      return { type: 'error' as const, text: t('health.error') }
    default:
      return { type: 'default' as const, text: t('health.checking') }
  }
})

const healthTooltip = computed(() => {
  const url = getBackendUrl() || t('health.sameOrigin')
  if (health.state === 'connected' && health.info) {
    return `${url} · v${health.info.version} · ${health.info.decoder}`
  }
  return health.error ? `${url} — ${health.error}` : url
})

function toggleLocale(): void {
  const next: AppLocale = locale.value === 'en' ? 'zh-CN' : 'en'
  locale.value = next
  document.documentElement.lang = next
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, next)
  } catch {
    // storage unavailable: the choice lasts for this page only
  }
}

onMounted(() => {
  document.documentElement.lang = locale.value
  health.startPolling()
  if (desktop) void recorder.init()
})
onBeforeUnmount(() => health.stopPolling())
</script>

<template>
  <NConfigProvider :theme="theme" :locale="naiveLocale" :date-locale="naiveDateLocale">
    <NGlobalStyle />
    <NMessageProvider>
      <NDialogProvider>
        <NLayout class="app-layout">
          <NLayoutHeader bordered class="app-header">
            <RouterLink to="/upload" class="app-title">{{ t('app.title') }}</RouterLink>
            <NMenu mode="horizontal" :options="menuOptions" :value="activeKey" class="app-menu" />
            <NTooltip>
              <template #trigger>
                <NTag
                  :type="healthTag.type"
                  size="small"
                  round
                  class="health-tag"
                  data-testid="health-indicator"
                  :data-state="health.state"
                  @click="health.check()"
                >
                  {{ healthTag.text }}
                </NTag>
              </template>
              {{ healthTooltip }}
            </NTooltip>
            <NButton size="small" quaternary data-testid="locale-toggle" @click="toggleLocale">
              {{ locale === 'en' ? '中文' : 'EN' }}
            </NButton>
          </NLayoutHeader>
          <NLayoutContent class="app-content">
            <NAlert
              v-if="health.unauthorized && route.name !== 'settings'"
              type="warning"
              closable
              class="auth-banner"
              data-testid="auth-banner"
              @close="health.clearUnauthorized()"
            >
              {{ t('errors.unauthorized') }}
              <RouterLink :to="{ name: 'settings' }">{{ t('errors.goToSettings') }}</RouterLink>
            </NAlert>
            <RouterView />
          </NLayoutContent>
        </NLayout>
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>

<style>
html,
body,
#app {
  margin: 0;
  height: 100%;
}

.app-layout {
  min-height: 100vh;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 24px;
  height: 56px;
}

.app-title {
  font-weight: 600;
  font-size: 16px;
  color: inherit;
  text-decoration: none;
  white-space: nowrap;
}

.app-menu {
  flex: 1;
}

.health-tag {
  cursor: pointer;
}

.app-content {
  padding: 24px;
}

.nav-rec-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-left: 6px;
  border-radius: 50%;
  background: #d03050;
  vertical-align: middle;
}

.auth-banner {
  max-width: 1080px;
  margin: 0 auto 16px;
}
</style>
