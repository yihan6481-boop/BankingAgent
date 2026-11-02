# 服务器部署说明

本目录提供现有 Linux Docker Compose 部署方案。清洁包保留业务代码、Compose、Dockerfile、配置模板和依赖锁。本次材料补充了用户执行的voicefix1部署输出及手机语音反馈，详见 ../../docs/VOICE_ACCEPTANCE.md；不表示全部业务或空环境部署已验收。

## 组件和入口

| 组件 | 容器端口 | 说明 |
|---|---|---|
| Nginx | 80、443 | HTTPS 主站和模拟短信子域 |
| bank | 8039 | 宿主机仅回环映射，入口 tools/server_entry.py |
| sms | 8040 | 输入银行安全手机号查看短信的模拟接收器 |
| memory | 18889 | 独立 Hindsight 服务与记忆库 |
| gateway | 18761 | 模型网关与累计计量账本 |
| database | 5432 | PostgreSQL 业务与记忆存储，不公开主机端口 |

服务器统一使用 `tools/server_entry.py --component bank|sms|memory|gateway --config <工程外配置>`。模型、OCR、记忆、数据库和支付凭据保存在工程外，各服务仅挂载所需配置。模板中的占位值不能直接运行。

## 首次空环境与已有环境

当前 restore 路线依赖私密数据库备份及配套密钥、账本、身份记录。公开源码包不含这些内容，不能把作者备份恢复当作评委独立复现流程。

首次空环境还需完成并验证：数据库和受限角色创建、30 份业务迁移、合成数据与随机演示凭据、记忆库和服务鉴权、模型网关及费用配置、模型缓存、域名证书与代理。现有 demo 数据生成与 PG 管理脚本已保留，但尚未封装并验证成适用于本服务器入口的完整首次安装。这些环境准备和首次启动验证仍待补齐。

本地已有 PostgreSQL 环境可使用根目录 run_integrated 入口，默认读取用户目录 `.banking-agent/pg17/pgpass.conf`。该入口和服务器入口不是可互换的空环境安装脚本。

## 已配置环境的检查和启动

现有服务器包含多份Compose覆盖，必须保留。实际环境文件由运行中bank容器的 `com.docker.compose.project.environment_file` 标签确定，不能直接套用不存在的通用路径。用户当前文件为 `/srv/banking/config/deployment-20261031-154329/compose.env`。

完整只读检查、语音配置、应用升级、回退及Nginx入口检查见 [VOICE_DEPLOY.md](../../docs/VOICE_DEPLOY.md)。当前r3仅同步材料，已运行的voicefix1无须重复部署。

服务缺依赖时定位原因，不关闭权限、关闭记忆、重建原库或清零费用绕过。镜像标签不能代替运行时指纹，configured=true不能代替真实录音验证。

## 发布身份与验收

`SOURCE_PACKAGE.json` 标识源码清洁包，`MANIFEST.json` 验证交付字节。`SERVER_VERSION.json` 的 deployment_id 为兼容原环境保留；配置中的发布身份、数据库 restore identity 和真实加载版本仍需部署时核对。本次不自动修改私密配置。

检查 `/health/deployment` 的 deployment_id、build、ui_sha256；内部 `/health/ready` 检查数据库身份、迁移、记忆、网关与账本。HTTP 200 不等于付款、真实模型或 OCR 链路全部通过。登录、本人只读、确认、回执和 SSE 需分别留证。

## 已有服务器升级与回滚

在工程外保存代码版本、配置和持久数据。升级前核对现有挂载、数据库身份、迁移、镜像及共享账本，停止所有共享状态的写入方并一致备份，再在独立版本目录构建和切换。`ops.py` 提供受控备份/恢复；restore 仅面向确认过的新空集群，不能覆盖已有库。

不得修改已应用 SQL 迁移校验值、重置预算、降低记忆撤回 fence，或把未知付款当作失败再次提交。代码回滚优先恢复已核对镜像和配置；不能用旧数据快照直接覆盖后续交易。私钥、密码、真实备份和费用账本不得进入公开源码仓库。
