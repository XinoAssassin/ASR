import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { i18n } from './locales'
import { router } from './router'

createApp(App).use(createPinia()).use(router).use(i18n).mount('#app')
