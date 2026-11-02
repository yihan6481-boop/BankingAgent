# Banking Agent

自然语言银行智能体：前端使用 React、TypeScript、Vite，后端使用 FastAPI、Pydantic 和 PostgreSQL。覆盖模拟转账、账单、理财、卡片、订阅和跨场景任务；权限、确认、强验证、幂等、审计与回执核验由服务端执行。项目不连接真实银行或支付清算网络。

本包为 **final5 memoryfix1 运行源码精简版，2026-11-02**。保留当前业务源码、前端构建产物、依赖锁、SQL 迁移、初始化规格及部署入口。已删除测试集、测试夹具、历史验收记录、截图、旧案例、独立评测脚本及旧命令行入口。原生记忆启动器只保留正式 runtime 模式。

## 启动与构建

先验证解压后的交付文件：

```bash
python -B deploy/server/verify_package.py .
```

完整启动需要准备 PostgreSQL、受限角色、迁移、演示数据、工程外配置，以及模型和记忆服务。**当前仍未提供经过空白主机验证的一键部署流程。** 本次是文件精简，不代表完成真实数据库、模型或端到端验收。

- 已有本地环境：`run_integrated.bat` / `run_integrated.ps1`，或 `python tools/run_integrated.py`。
- 已配置服务器：[部署说明](deploy/server/README.md)，入口 `tools/server_entry.py`。
- 前端构建：[frontend/README.md](frontend/README.md)。保留现有 `frontend/dist/`，供后端和 Docker 使用。
- 初始化数据：[demo/README.md](demo/README.md)。规格用于构造合成银行数据，不是旧测试集或服务器快照。
- 用户界面：`/ui/`；人工坐席：`/ui/?workspace=seat`。

## 文件结构

| 路径 | 用途 |
|---|---|
| `backend/` | 当前业务、安全、API、持久任务、记忆接入及数据库迁移 |
| `frontend/` | 前端源码、构建配置、锁文件和现有 dist |
| `deploy/server/` | 当前服务器部署、配置模板、健康检查及运维入口 |
| `deploy/r4/` | 模型网关及固定依赖版本 |
| `tools/` | 当前启动、短信、原生记忆及数据初始化工具 |
| `demo/spec/` | 数据初始化使用的合成数据规格 |
| `manual/` | 当前票据审核页面 |

数据库迁移、安全审计代码、运行时健康检查仍是系统组成部分，不能按“旧文件”删除。程序运行时会产生新日志和账本；本包不携带旧记录。

`SOURCE_PACKAGE.json` 标识本次源码包；`MANIFEST.json` 校验交付字节；`SERVER_VERSION.json` 的 deployment_id 沿用原配置兼容标识，不代表已部署本包。删减评测模块后源码指纹发生变化，部署时需核对新指纹。

本包按要求不含测试用例。比赛要求的最新版测试材料需另行准备；技术报告、安全自评、PPT 与视频单独提交。私密配置、密码、数据库备份和实际会话不得纳入公开源码。
