import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: () => import('@/views/HomeView.vue') },
  { path: '/upload', name: 'upload', component: () => import('@/views/UploadView.vue') },
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
