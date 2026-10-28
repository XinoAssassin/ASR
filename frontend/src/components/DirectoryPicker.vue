<script setup lang="ts">
/**
 * Target directory for uploads: root select (`/api/fs/roots`) + lazily loaded subdirectory tree
 * (`/api/fs/dirs`) + manual path input + recent directories. The server validates the path
 * against ASR_UPLOAD_ROOTS (rule 2); this component only helps pick one.
 */
import { NAlert, NButton, NInput, NSelect, NSpace, NTag, NText, NTree, type TreeOption } from 'naive-ui'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { listDirs, listRoots, type FsDir, type FsRoot } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'

const model = defineModel<string>({ required: true })
defineProps<{ recent: string[] }>()

const { t } = useI18n()
const { describe } = useApiErrors()

const roots = ref<FsRoot[]>([])
const rootsError = ref<string | null>(null)
const root = ref<string | null>(null)
const tree = ref<TreeOption[]>([])
const treeLoading = ref(false)
const treeError = ref<string | null>(null)

const rootOptions = computed(() =>
  roots.value.map((r) => ({ label: `${r.name} — ${r.path}`, value: r.path })),
)

function toNodes(dirs: FsDir[]): TreeOption[] {
  return dirs.map((d) => ({ key: d.path, label: d.name, isLeaf: false }))
}

async function loadRoots(): Promise<void> {
  try {
    roots.value = await listRoots()
    rootsError.value = null
    const current = model.value
    const match = roots.value.find((r) => current && (current === r.path || current.startsWith(r.path)))
    const first = match ?? roots.value[0]
    if (first) {
      root.value = first.path
      if (!current) model.value = first.path
      await loadTree(first.path)
    }
  } catch (e) {
    rootsError.value = describe(e)
  }
}

async function loadTree(path: string): Promise<void> {
  treeLoading.value = true
  treeError.value = null
  try {
    tree.value = toNodes((await listDirs(path)).dirs)
  } catch (e) {
    tree.value = []
    treeError.value = describe(e)
  } finally {
    treeLoading.value = false
  }
}

async function onLoad(node: TreeOption): Promise<void> {
  try {
    const dirs = (await listDirs(String(node.key))).dirs
    node.children = toNodes(dirs)
    if (!dirs.length) node.isLeaf = true
  } catch (e) {
    node.children = []
    node.isLeaf = true
    treeError.value = describe(e)
  }
}

function onRootChange(path: string): void {
  root.value = path
  model.value = path
  void loadTree(path)
}

function onSelect(keys: Array<string | number>): void {
  const key = keys[0]
  if (key != null) model.value = String(key)
}

const selectedKeys = computed(() => (model.value ? [model.value] : []))

onMounted(() => void loadRoots())
</script>

<template>
  <NSpace vertical data-testid="directory-picker">
    <NAlert v-if="rootsError" type="warning" :show-icon="false">
      {{ t('upload.dir.rootsError') }}：{{ rootsError }}
    </NAlert>
    <NSpace v-else :wrap="false" align="center">
      <NSelect
        :value="root"
        :options="rootOptions"
        :placeholder="t('upload.dir.root')"
        data-testid="root-select"
        style="min-width: 320px"
        @update:value="onRootChange"
      />
      <NButton quaternary :disabled="!root" @click="root && loadTree(root)">
        {{ t('common.refresh') }}
      </NButton>
    </NSpace>
    <div v-if="root" class="tree-box">
      <NText v-if="treeError" type="error" depth="3">{{ treeError }}</NText>
      <NText v-else-if="!treeLoading && !tree.length" depth="3">{{ t('upload.dir.empty') }}</NText>
      <NTree
        v-else
        block-line
        :data="tree"
        :on-load="onLoad"
        :selected-keys="selectedKeys"
        data-testid="dir-tree"
        @update:selected-keys="onSelect"
      />
    </div>
    <NInput
      v-model:value="model"
      :placeholder="t('upload.dir.manual')"
      clearable
      data-testid="target-dir-input"
    >
      <template #prefix>{{ t('upload.dir.target') }}</template>
    </NInput>
    <NSpace v-if="recent.length" align="center" size="small">
      <NText depth="3">{{ t('upload.dir.recent') }}</NText>
      <NTag
        v-for="dir in recent"
        :key="dir"
        size="small"
        :type="dir === model ? 'primary' : 'default'"
        class="recent-tag"
        @click="model = dir"
      >
        {{ dir }}
      </NTag>
    </NSpace>
  </NSpace>
</template>

<style scoped>
.tree-box {
  max-height: 240px;
  overflow: auto;
  border: 1px solid var(--n-border-color, rgba(128, 128, 128, 0.25));
  border-radius: 4px;
  padding: 4px 8px;
}

.recent-tag {
  cursor: pointer;
}
</style>
