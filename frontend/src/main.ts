import { createPinia } from 'pinia'
import { createApp } from 'vue'

import { onAuthorized, onUnauthorized } from './api'
import App from './App.vue'
import { i18n } from './locales'
import { router } from './router'
import { useHealthStore } from './stores/health'

const pinia = createPinia()
const app = createApp(App).use(pinia).use(router).use(i18n)

// Any 401 from any transport raises the "set your API key" banner.
onUnauthorized(() => useHealthStore(pinia).markUnauthorized())
// ...and a successful authenticated call (key fixed, server restarted without auth) clears it.
onAuthorized(() => useHealthStore(pinia).clearUnauthorized())

app.mount('#app')
