<script setup lang="ts">
import {
  type MenuOption,
  NConfigProvider,
  NLayout,
  NLayoutContent,
  NLayoutHeader,
  NMenu,
  NMessageProvider,
  NTag,
  dateEnUS,
  dateZhCN,
  enUS,
  zhCN,
} from 'naive-ui'
import { computed, h, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import { useHealthStore } from '@/stores/health'

const { t, locale } = useI18n()
const route = useRoute()
const health = useHealthStore()

const naiveLocale = computed(() => (locale.value === 'en' ? enUS : zhCN))
const naiveDateLocale = computed(() => (locale.value === 'en' ? dateEnUS : dateZhCN))

const navItems = [
  { key: 'upload', to: '/upload', label: 'nav.upload' },
  { key: 'jobs', to: '/jobs', label: 'nav.jobs' },
  { key: 'settings', to: '/settings', label: 'nav.settings' },
] as const

const menuOptions = computed<MenuOption[]>(() =>
  navItems.map((item) => ({
    key: item.key,
    label: () => h(RouterLink, { to: item.to }, { default: () => t(item.label) }),
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

onMounted(() => {
  void health.check()
})
</script>

<template>
  <NConfigProvider :locale="naiveLocale" :date-locale="naiveDateLocale">
    <NMessageProvider>
      <NLayout class="app-layout">
        <NLayoutHeader bordered class="app-header">
          <RouterLink to="/" class="app-title">{{ t('app.title') }}</RouterLink>
          <NMenu mode="horizontal" :options="menuOptions" :value="activeKey" class="app-menu" />
          <NTag :type="healthTag.type" size="small" round>{{ healthTag.text }}</NTag>
        </NLayoutHeader>
        <NLayoutContent class="app-content">
          <RouterView />
        </NLayoutContent>
      </NLayout>
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

.app-content {
  padding: 24px;
}
</style>
