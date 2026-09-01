# TODO 小组件 · 工程指南（面向 AI Agent / AI Harness）

> 本文档面向**后续接手本工程的 AI Agent / AI Harness 开发者**，描述架构、数据流、HTTP API 契约、已知坑与修改规范。运行本工程前请通读全文，尤其是「⚠️ 关键工程决策」与「已知坑」两节，避免重蹈覆辙。

---

## 1. 项目概览

一个**桌面 TODO 小组件**：单文件 HTML 待办应用 + Python 启动器，运行在 Windows 上。

| 项 | 值 |
|---|---|
| 功能 | 多列表待办、文字/图片任务、拖拽排序、完成态反选、分类标签拖拽、原图灯箱查看、外部查看器打开原图 |
| 架构 | **pywebview 6.2.1 + WebView2** 无边框宿主 + 本地 HTTP 服务（127.0.0.1:8765） |
| 前端 | 单文件 `todo-widget.html`（纯 JS，无构建步骤，内嵌 CSS/JS） |
| 后端 | `serve.py`：HTTP 服务 + 托盘驻留 + 快捷方式安装 |
| 数据 | localStorage（缓存）+ IndexedDB（图片原图）+ **服务端 JSON 文件（权威持久化）** 三层 |
| 历史 | 曾用 Edge `--app` 模式，因 Chromium 自绘标题栏无法剥离而迁移至 pywebview frameless |

## 2. 目录结构

| 文件 | 作用 | 是否可生成 |
|---|---|---|
| `serve.py` | 启动器 + HTTP 服务 + 托盘（**核心后端**） | 源码 |
| `todo-widget.html` | 应用主体（**核心前端**，单文件） | 源码 |
| `widget-data.json` | **旧版单文件数据（迁移源）**，首次启动自动拆分为 state/images，原文件归档为 `.migrated` | 运行时生成 |
| `widget-state.json` | 拆分后的小状态文件（{version, state, ui}，几 KB，每次保存全量重写） | 运行时生成 |
| `widget-images.json` | 拆分后的图片文件（{imageId: dataUrl}，仅图片增删时写） | 运行时生成 |
| `widget-backup.json` | 一次性迁移备份（WebView2 迁移用，被 `/__restore__` 消费后删除） | 运行时生成 |
| `widget-backup.json.bak` | 手动保留的备份副本（**当前唯一离线数据源，勿删**） | 手动 |
| `85037897_p0.ico` | 托盘/快捷方式图标 | 资源 |
| `安装桌面小组件.bat` | 一键安装（调用 `serve.py --install`） | 脚本 |
| `TODO小组件-开发方案.md` | 早期开发方案文档 | 文档 |
| `.workbuddy/memory/` | 工作日志（按日期追加，含全部技术决策与踩坑记录） | 文档 |

## 3. 环境与启动

| 项 | 说明 |
|---|---|
| 生产 Python | `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe`（Python 3.13.14，已装 pywebview 6.2.1 / pystray / pillow / pythonnet 3.1.0） |
| 工具 Python | `C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe`（语法检查用） |
| 启动 | `pythonw serve.py`（或双击桌面快捷方式「TODO小组件」） |
| 安装 | `python serve.py --install`（重建桌面快捷方式 + 开机启动项，快捷方式 Target 指向 venv pythonw） |
| 调试 | `python serve.py --debug`（前台运行打印日志） |

### 启动流程（`serve.py` main）

1. 单实例检测（socket 绑定 8765，失败则唤醒已有实例的 `/__act__`）
2. 启动 `ThreadingHTTPServer`（127.0.0.1:8765，静态服务根目录 = BASE）
3. `webview._state["storage_path"] = %APPDATA%\TODO小组件\WebView2`（**必须显式设置**，否则 private_mode 导致缓存临时化）
4. `webview.create_window(frameless=True, resizable=False)` → 窗口 464×738，无标题栏、不占任务栏（WS_EX_TOOLWINDOW）
5. pystray 托盘驻留（双击显隐 / 右键退出）

## 4. HTTP API 契约

服务只绑定 `127.0.0.1`。**除 `/__ping__`、`/__act__` 外，所有数据端点都做 Origin 校验**（见 §6 鉴权）。

### GET

| 端点 | 语义 | 返回 |
|---|---|---|
| `/__ping__` | 存活探活（serve.py 内部 urllib 调用，不带头） | 200 |
| `/__act__` | 激活/显示已有窗口（单实例唤醒用，不校验） | 200 |
| `/__quit__` | 优雅退出（204 → 服务端 1.2s 后强制兜底退出 pythonw） | 204 |
| `/__mtime__` | 返回 `todo-widget.html` 的 mtime（前端热更新轮询用，2.5s/次） | `{"mtime": <float>}` |
| `/__data__` | 日常持久化数据：合并 `widget-state.json` + `widget-images.json` | 200 + `{version, state, ui, images}`；无数据 204 |
| `/__restore__` | 一次性迁移备份：存在 `widget-backup.json` 则返回其内容**并立即删除文件** | 200 + 备份 JSON；无备份 204 |

### POST（均要求 `Content-Type: application/json`）

| 端点 | 请求体 | 语义 |
|---|---|---|
| `/__save__` | `{state, ui, images?}` | 保存状态：只重写 `widget-state.json`，images 仅增量合并 |
| `/__image__` | `{imageId, dataUrl}` | 图片上传：写 `widget-images.json` |
| `/__image_del__` | `{imageId}` | 图片删除：从 images 文件移除（不存在则无副作用） |
| `/__view_image__` | `{imageId?, dataUrl?}` | 把图片写入 %TEMP% 临时文件并用系统查看器打开（优先取服务端原图，取不到用传入 dataUrl 兜底）；60s 后自动清理临时文件 |
| `/__backup__` | `{state, ui, images}` | 把备份写入 `widget-backup.json`（一次性迁移入口） |

### 响应约定

- 成功：200（GET 数据类 200 + body；POST 200 空 body）
- 无数据：204（GET `/__data__`、`/__restore__`）
- 拒绝：403（来源校验失败，先排空请求体再响应，避免客户端连接中止）
- 参数错误：400；服务端异常：500
- 所有响应带 `Cache-Control: no-store`（前端 fetch 均带 `cache:'no-store'`）

## 5. 数据流与持久化（核心）

```
[HTML 内存 state] ──save() 防抖400ms──▶ localStorage（缓存，重启可能丢）
      │                                        │
      │ save()/persistImage()/persistImageDel()│
      ▼                                        ▼
[本地服务 widget-state.json + widget-images.json]  ◀── 权威持久化兜底
      ▲
      │ 启动时 localStorage 为空
      │ migrate(): fetch /__data__ → 无则 /__restore__ → 接管内存 → render()
      └─────────────────────────────────────────
```

**关键规则（AI 修改时必须遵守）**：

1. **localStorage 不是权威数据源**。pywebview + WebView2 沙盒下 localStorage/IndexedDB **跨 reload、跨重启不持久**（已实测证实，见 §8）。前端任何状态变更必须通过 `save()`/`persistImage()`/`persistImageDel()` 同步到服务端。
2. **恢复链路禁止 `location.reload()`**。reload 会清空 localStorage，导致「恢复成功后数据又丢」的假象（历史教训）。正确做法：fetch 拿到数据 → 直接接管内存 `state = d.state` → `render()`。
3. 恢复源优先级：`/__data__`（日常）> `/__restore__`（一次性迁移）。
4. 图片任务：原图存 IndexedDB（`todoWidgetDB`）**并**上传服务端（`widget-images.json`）；删除任务 5 秒确认后同步删除两端。
5. 数据文件全部**原子写**（tmp → `os.replace`），所有读-改-写必须持 `DATA_LOCK`（RLock），否则并发丢图。
6. 旧单文件 `widget-data.json` 由 `_ensure_migrated()` 自动拆分迁移，原文件**归档为 `.migrated`（绝不删除）**。

## 6. 鉴权（Origin 校验）

- `_from_widget()`：校验请求头 `Origin` 或 `Referer` 前缀 === `http://127.0.0.1:8765`（端口来自 `ALLOWED_ORIGIN`）。
- 应用范围：GET `/__data__`、`/__restore__`；**所有** POST。
- 豁免：`/__ping__`、`/__act__`（serve.py 内部 urllib 调用不带头）。
- 效果：本机恶意网页/程序无法读写数据；浏览器跨域 POST 还会被 CORS 预检（501）双保险拦截。
- **注意**：请求带 `Origin: null` 或 `file://` 来源会被拒绝——前端必须通过 `http://127.0.0.1:8765` 页面访问。

## 7. 前端关键实现（todo-widget.html）

| 符号 | 说明 |
|---|---|
| `STORAGE_KEY = 'todoWidgetData_v1'` / `UI_KEY = 'todoWidgetUI_v1'` | localStorage 键 |
| `todoWidgetDB`（IndexedDB v1） | 图片原图存储；`idbPut`/`idbGet`/`idbDel` 封装 |
| `state` | 内存主状态：`{lists:[{id,name,tasks:[...]}]}`；任务含 `type:'text'|'image'`、`imageId`、`done` 等 |
| `save()` | localStorage 写入 + **防抖 400ms 同步服务端** |
| `migrate()`（IIFE） | 启动恢复：备份模式（`?backup=1`）→ 日常恢复（/__data__ → /__restore__） |
| `syncToServer(extraImages)` / `persistImage` / `persistImageDel` | 服务端同步 |
| `render()` → `renderLists() + renderTasks() + attachListbarNav()` | 全量渲染 |
| 热更新 | 2.5s 轮询 `/__mtime__`；检测到 HTML 变更时：若焦点在 input/textarea/contenteditable 或灯箱打开 → **延后刷新**；失焦/关灯箱后立即刷新；刷新前先 `save()` |

**修改 HTML 后**：用户端通过 `/__mtime__` 热更新自动生效（无需手动刷新）；但 serve.py 修改后**必须重启小组件**（pythonw 进程加载的是启动时代码）。

## 8. ⚠️ 已知坑（务必阅读）

| # | 坑 | 现象 | 对策 |
|---|---|---|---|
| 1 | **WebView2 沙盒下 localStorage/IDB 跨 reload/重启不持久** | 数据恢复后刷新即丢；日常新增任务重启即丢 | 服务端文件兜底（§5）；禁止依赖 localStorage 持久性 |
| 2 | **WebView2 黑屏**（GPU 渲染失败） | 窗口 0×0 或全黑，页面不加载 | `edgechromium.py` 的 `AdditionalBrowserArguments` 追加 `--disable-gpu --disable-gpu-compositing --disable-software-rasterizer --disable-features=VizDisplayCompositor --no-sandbox`。**venv 重建后需重打此补丁** |
| 3 | **`webview.settings` 不允许新增键** | 设置 storage_path 报错 | 用 `webview._state["storage_path"]` |
| 4 | **并发写竞态** | 快速连续上传多图时后写覆盖先写 → 丢图 | 所有读-改-写包 `DATA_LOCK`（RLock） |
| 5 | **Edge headless 忽略 `--user-data-dir`** | 系统已有 Edge 实例时复用真实 profile，读到真实数据 | 隔离测试必须用 Chrome headless（独立进程） |
| 6 | **沙盒后台进程 `os.remove()` 被拦截** | 后台进程内删除文件导致连接重置 | 涉及文件删除的验证必须前台跑 |
| 7 | **恢复逻辑 `location.reload()`** | reload 清空 localStorage → 数据"永久丢失"假象 | 去掉 reload，直接接管内存渲染（§5 规则 2） |
| 8 | **窗口坍缩 0×0 / 渲染 workbuddy.cn** | WebView2 host 被其他程序（如安装器）占用 | 杀掉 msedgewebview2 残留进程后重启 |
| 9 | **pythonw 无控制台** | print 崩溃 | 模块头部 stdout/stderr 重定向到 os.devnull |

## 9. 修改与验证规范

1. **改 serve.py**：`py_compile` 语法检查 → 用户需重启小组件生效。
2. **改 todo-widget.html**：提取 `<script>` 内 JS 用 `node --check` 语法检查 → 经 `/__mtime__` 热更新自动生效。
3. **接口验证**：必须用**独立端口（如 8899）+ 临时数据目录**（monkey-patch `serve.PORT` / `STATE_FILE` / `IMAGES_FILE` 等），**绝不触碰用户桌面上运行中的 8765 实例与真实数据文件**。起测试服务前先 `netstat` 确认端口空闲。
4. **端到端验证**：Chrome headless + CDP（`--user-data-dir` 隔离 profile），注意 Edge headless 单例坑（见 §8#5）。
5. **数据校验**：JSON 结构校验脚本（列表/任务/图片引用一致性、无孤儿图）只读不改。

## 10. 已知限制与后续建议

| 项 | 现状 | 建议方向 |
|---|---|---|
| 数据文件 | state 与 images 分离，images 全量 base64 在内存中读写 | 图片量大时可改文件路径引用（存 %APPDATA%） |
| `/__view_image__` | 临时文件 60s 后自动清理 | 已解决，无需处理 |
| 鉴权 | Origin 前缀校验（非密码级） | 单用户本机场景足够 |
| 热更新 | 输入/灯箱中延后刷新，无超时强制 | 开发场景合理 |
| 迁移备份 | `widget-backup.json` 消费后删除，`.bak` 手动保留 | 保留 `.bak` 作为离线恢复源 |

## 11. 快速自检清单（改动后）

- [ ] `python -m py_compile serve.py` 通过
- [ ] HTML 内嵌 JS `node --check` 通过
- [ ] 未触碰用户真实数据文件（`widget-data.json` / `widget-state.json` / `widget-images.json` / `*.bak`）
- [ ] 测试使用独立端口 + 临时数据目录
- [ ] 恢复链路无 `location.reload()`
- [ ] 所有数据端点读-改-写持锁
- [ ] 数据文件原子写（tmp → replace）
