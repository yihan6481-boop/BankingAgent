# 定向测试与复现

统计测试 tests/test_risk_statistics.py 为17/17通过；语音状态机12/12、配置测试9/9及4个模拟DOM场景通过。离线测试不调用数据库或真实转写服务。用户在服务器执行的部署与真机反馈另见VOICE_ACCEPTANCE.md，不能与模拟测试混为一类。

## 运行方式

在源码包根目录运行：

```bash
python -B -m unittest discover -s tests -v
python -B deploy/server/verify_package.py .
```

Python 3.11或更新版本，需要 Asia/Shanghai 时区数据。Linux通常由系统提供；Windows若提示 ZoneInfoNotFoundError，可在隔离虚拟环境运行 `python -m pip install tzdata` 后重试。本轮使用Python 3.12.14。测试只使用标准库。

## 方法与覆盖

通过AST从 backend/assistance/risk.py 提取实际 statistics 函数后执行，没有复制一份替代算法，也不导入周围数据库依赖。给定金额均为分，测试使用固定时间与合成历史。

| 测试边界 | 项数 |
|---|---:|
| 空历史、7笔、8笔、等于金额阈值 | 4 |
| MAD为零时的100元备用增量与3倍分位数 | 2 |
| 首次收款人、已有收款人 | 2 |
| 排除未来记录及非正金额 | 1 |
| 时段11笔、12笔、恰好10%、排除旧合成时间 | 4 |
| 频次2笔、3笔、恰好10分钟、超出窗口 | 4 |
| 合计 | 17 |

## 结论边界

以上仅证明统计纯函数在这些输入下返回预期结果，不覆盖数据库查询范围、evaluate的任务绑定、语义线索引用校验、HTTP接口、模型判断、支付授权、短信、交易原子性或上线可用性。不能转换成“反诈准确率100%”或“系统全测通过”。原始历史测试集、日志和数据库备份未恢复进包。

## 语音定向测试

先在干净源码包运行清单检查；重新构建前端会改变交付字节，不应再用原清单认定原包损坏。

```bash
python -B deploy/server/verify_package.py .
python -B -m unittest deploy.server.test_voice_configuration -v
cd frontend
npm ci --ignore-scripts --no-audit --no-fund
node --test scripts/test-voice.mjs
node scripts/test-voice-panel.mjs
npm run build
```

服务端配置测试需要依赖锁中的httpx、psycopg等运行依赖，建议使用已有隔离开发环境。状态机测试模拟识别事件和时钟；DOM脚本使用jsdom及模拟MediaRecorder/供应商，验证点击、取消、单次上传、仅回填草稿及未配置提示。9项配置测试核对真实install加载逻辑、敏感字段不泄漏和格式校验，不调用真实供应商。

12项、9项、4场景与17项的覆盖范围不同，不将其合并为“42项端到端测试”。当前材料同步未修改语音实现或重建前端，保留既有voicefix1产物。
