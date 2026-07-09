---
kind: frontend_style
name: 前端样式系统：无独立 UI，文档站点内联样式
category: frontend_style
scope:
    - '**'
source_files:
    - docs/index.html
---

本仓库是一个基于 asyncio 的电梯控制核心（Python 后端），**不包含任何前端应用代码、CSS 框架或组件库**。唯一的 HTML 页面位于 `docs/index.html`，是项目设计哲学与架构说明文档，其样式采用以下方式组织：

1. **样式组织形式**：所有 CSS 以 `<style>` 标签内联嵌入在单个 HTML 文件中，未使用外部 `.css`/`.scss`/`.less` 文件，也未引入 Tailwind、Bootstrap 等任何样式框架。
2. **设计令牌（Design Tokens）**：通过 CSS `:root` 自定义属性集中定义，包括深色主题背景色（`--bg-0` ~ `--bg-2`）、文字色（`--text-0` ~ `--text-2`）、三层架构语义色（`--brain` 青绿=大脑、`--cere` 橙=小脑、`--stem` 紫=脑干、`--cron` 粉=cron）以及警告/错误色。这些变量贯穿全站，形成统一的视觉语言。
3. **响应式策略**：仅使用少量 `@media (max-width: ...)` 断点（780px、900px、1100px）配合 `clamp()` 函数实现基础响应式布局，无移动端优先或桌面端优先的明确倾向。
4. **排版约定**：正文使用系统字体栈（-apple-system / PingFang SC / Microsoft YaHei），代码使用 JetBrains Mono/Fira Code；标题层级 h1~h4 统一字号与颜色；表格、卡片、时间线等文档元素均有对应类名（如 `.triad`、`.layer`、`.step`、`.phi-table`）。
5. **交互效果**：仅包含滚动浮现（`.reveal` + JS 切换 `.in` 类）、悬停变色、粘性目录导航等轻量动效，无动画库依赖。

**结论**：本项目不存在面向用户的前端 UI 样式系统——`frontend_style` 类别在此仓库中不适用。HTML 中的样式仅为技术文档展示服务，不具备可复用性，也不构成项目的视觉设计体系。