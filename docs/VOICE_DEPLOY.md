# 语音部署与运行检查

已有线上语音已由用户部署并实测。当前r3是材料同步与GitHub交付版，程序与前端产物保持voicefix1，不需要仅为更新README再部署服务器。

## 实际部署路径与版本

用户终端提供的环境文件：`/srv/banking/config/deployment-20261031-154329/compose.env`。项目名：`banking-server`。工作目录：`/srv/banking/app/deploy/server`。

语音运行包目录：`/srv/banking/releases/final5-memoryfix1-sms-phone-voicefix1-20261102-7b8388b285c9`。这是已上传运行包路径，不是GitHub源码ZIP解压后的自动部署路径。

环境路径可能随部署变化，后续应先读取容器标签。不要把迁移备份中的.env作为当前环境，也不要使用不存在的 `/srv/banking/config/compose.env`。

```bash
sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.environment_file"}}' banking-server-bank-1
sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' banking-server-bank-1
sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' banking-server-bank-1
```

## 复用当前Compose进行只读检查

以下脚本不更新容器，不输出私有环境内容。它保留基础文件及全部覆盖文件，遇到路径缺失会停止。

```bash
bash <<'BASH'
set -euo pipefail
container=banking-server-bank-1
project=$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container")
workdir=$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$container")
envfile=$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.environment_file"}}' "$container")
configs=$(sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$container")
test -n "$project"
sudo test -d "$workdir"
sudo test -f "$envfile"
IFS=',' read -r -a config_list <<< "$configs"
compose=(sudo docker compose --project-name "$project" --project-directory "$workdir" --env-file "$envfile")
for config_file in "${config_list[@]}"; do
  sudo test -r "$config_file"
  compose+=(-f "$config_file")
done
"${compose[@]}" config --quiet
"${compose[@]}" ps
BASH
```

## 私有ASR配置

按 `/run/private/server.json` 中 `files.model_environment` 定位私有模型配置，在原有文件中增改以下字段，不覆盖LLM/VISION/R4设置。`model.env.example`保持默认禁用，启用需要服务商真实凭据。

```dotenv
BA_ASR_ENABLED=1
BA_ASR_ENDPOINT=https://api.siliconflow.cn/v1/audio/transcriptions
BA_ASR_MODEL=FunAudioLLM/SenseVoiceSmall
BA_ASR_API_KEY=<仅在服务器私有文件填写真实值>
BA_ASR_MIME_TYPES=audio/webm,audio/mp4,audio/ogg,audio/wav,audio/mpeg
```

实际格式需由供应商接受并在设备上测试；配置中的MIME清单不是供应商兼容性证明。保存原配置的受限备份并保持原所有者/权限。终端隐藏输入可使用 `getpass.getpass`；如显式打开终端流，使用 `open('/dev/tty','w')`，不可用要求随机访问的 `r+`。密钥不得放入命令历史、源码、前端、截图或GitHub。

私有配置变更后重启bank。只改私有配置不需要重新构建镜像。检查：

```bash
sudo docker exec banking-server-bank-1 python /app/deploy/server/voice_check.py --config /run/private/server.json
```

`configured=true`是配置检查，不代表网络、余额、模型权限或真实录音成功；须继续实际录音验收。

## 应用升级与回退

升级前校验运行包清单，保留旧镜像、旧目录、全部Compose文件及私有配置备份。使用新运行包的镜像覆盖追加到上述完整Compose数组后，再执行 `config --quiet`、`build bank`、`up -d --no-deps bank sms`。本次bank/sms共用语音修复镜像，不重建memory/gateway/database；无SQL迁移。

回退命令应基于升级前的完整Compose数组及原环境文件，执行 `up -d --no-deps bank sms`；若新旧配置不兼容，还需按保存的备份恢复对应配置。不要用旧数据库覆盖新交易，不要运行down -v、清库、清零预算或降低撤回fence。

源码包没有服务器运行包的release/image-tags.override.yaml。不要把完整源码或增量上传包直接覆盖现有运行目录；本次正常运行的服务器也无需再次更新。

## 502及入口验收

后端健康并不证明Nginx能够连接它。容器替换后，应检查代理配置、上游目标及网络地址，配置有效后重载并再次检查HTTPS。重载后的单次502不能证明最终故障原因。

```bash
sudo docker exec banking-server-bank-1 python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8039/health/ready", timeout=25).status)'
sudo docker exec banking-server-proxy-1 nginx -t
sudo docker exec banking-server-proxy-1 nginx -s reload
curl --fail --silent --show-error --max-time 20 -o /dev/null -w 'HTTP %{http_code}\n' https://bank.47-238-220-216.sslip.io/ui/
```

若仍502，检查Nginx实际proxy_pass/upstream和bank/proxy容器网络，再读取脱敏连接错误；不要反复重建数据库或更换语音密钥。最终须在手机刷新页面，录音、结束、草稿核对、取消，并确认无自动发送。
