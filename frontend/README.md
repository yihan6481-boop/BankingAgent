# Banking Agent 前端

React、TypeScript、Vite 移动端 H5。保留当前 `src/`、构建配置、`package-lock.json` 和现有 `dist/`；已移除旧 Mock 引擎、测试文件和测试命令。

在此目录安装与构建：

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run build
```

依赖版本和锁文件保持原样，开发依赖不等于已安装的依赖目录；本包不含 node_modules。首次安装需要可用的软件源。后端通过 `/ui/` 提供 dist，API 默认同源 `/api`。Dockerfile 使用 dist，因此不要删除。

修改源码后需重建 dist 并刷新交付清单。`npm run dev` 使用 Vite 开发服务，启动前核对代理目标。本次清理未重新构建前端，保留的运行源码和 dist 均与上一版相同。
