import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: { name: 'upload' } },
  { path: '/upload', name: 'upload', component: () => import('@/views/UploadView.vue') },
  // Desktop only; the view itself shows a "desktop app only" notice in the web build.
  { path: '/recorder', name: 'recorder', component: () => import('@/views/RecorderView.vue') },
  { path: '/jobs', name: 'jobs', component: () => import('@/views/JobsView.vue') },
  {
    path: '/transcripts/:id',
    name: 'transcript',
    component: () => import('@/views/TranscriptView.vue'),
    props: true,
  },
  { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})
