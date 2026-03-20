import { createRouter, createWebHistory } from 'vue-router'
import ProvidersView from './views/ProvidersView.vue'
import ProjectsView  from './views/ProjectsView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/',          redirect: '/providers' },
    { path: '/providers', component: ProvidersView },
    { path: '/projects',  component: ProjectsView  },
  ],
})
