<template>
  <router-view />
</template>

<script setup>
</script>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

:root.dark { color-scheme: dark; }

/* 暗色主题，对齐 mercari_manage */
html.dark {
  --el-bg-color: var(--bg-page);
  --el-bg-color-page: var(--bg-page);
  --el-fill-color-blank: var(--bg-card);
  --el-fill-color-light: var(--bg-hover);
  --el-mask-color: rgba(11, 18, 32, 0.78);
}

html, body, #app {
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  background: var(--bg-page);
  color: #e5e7eb;
}

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: #3a4456; border-radius: 3px; }
::-webkit-scrollbar-track { background: transparent; }

.el-card, .el-dialog, .el-table, .el-input__wrapper,
.el-select__wrapper, .el-textarea__inner { border-color: var(--border) !important; }

/* 工具栏日期区间筛选：收窄，别霸占整条工具栏（弹性容器里默认会被拉伸） */
.flt-date.el-date-editor.el-date-editor--daterange { width: 244px !important; flex: 0 0 244px !important; }
.flt-date .el-range-separator { padding: 0 2px; flex: none; }

/* 集运点选下拉：选项含状态标签的两行内容，改为自适应高度（默认 34px 会夹住） */
.ship-pop .el-select-dropdown__item { height: auto; line-height: 1.3; padding: 6px 12px; }

.el-card { background: var(--bg-card) !important; color: var(--txt-1); }
.el-card__header { background: var(--bg-head) !important; border-bottom: 1px solid var(--border) !important; color: var(--txt-1) !important; }
.el-card__body { background: transparent !important; color: var(--txt-1); }

.el-loading-mask { background-color: var(--el-mask-color) !important; }

.el-table {
  --el-table-header-bg-color: var(--bg-hover);
  --el-table-tr-bg-color: var(--bg-card);
  --el-table-row-hover-bg-color: var(--bg-row-hover);
  --el-table-border-color: var(--border);
  color: var(--txt-body);
}

.el-dialog { --el-dialog-bg-color: var(--bg-card); }
</style>
