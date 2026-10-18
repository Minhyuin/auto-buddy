# AutoBuddy

面向单个管理员的 WorkBuddy 多账户自动签到门户。采用 **Django + PostgreSQL + Docker Compose + Caddy**，目标服务器为 Ubuntu 22.04 x86_64。

## 做什么

- 管理多个 WorkBuddy 账户，新增、替换凭据、编辑备注、停用或删除。
- 每个账户独立选择「仅签到」或「签到 + 成长中心」，设置每日时间、IANA 时区和轮询间隔。
- 手动加入任务队列；查看经过脱敏的结果、失败/部分成功状态和执行器状态。
- 停机后合并补跑，不重放每一次错过的历史计划；同一账户最多一个排队或执行中的任务。
- 无效认证会暂停该账户，待替换凭据并重新启用。
- 通过正式 Release 构建不可变镜像，配套上游版本同步与主机端更新工具。

## 模式 B：不调用 AI，不修改上游

上游 [workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin) 是 Git submodule。AutoBuddy **只读使用它现有的 Python CLI**，不覆写上游文件，不调用模型、MCP 或桌面 AI 助手，也不需要 AI API Key。「只读」指不修改子模块源码，并不表示业务操作只读：签到和成长中心会向 WorkBuddy 发起领取/执行请求。当前固定到正式版 `v1.7.0`。

| 门户操作 | 上游 CLI | 行为 |
| --- | --- | --- |
| 在线认证检查 | `status` | 查询签到状态，不领取、不执行成长中心 |
| 仅签到 | `all` | 查询 + 签到，不进入成长中心 |
| 签到 + 成长中心 | `auto` | 执行上游确定性签到与成长中心流程 |

说明：`all` 是上游已有命令，可能向已签到账户再次发起幂等领取请求；上游自身有网络重试和活动规则判断。AutoBuddy 不复制其业务接口，不宣称禁用了上游内部重试。执行超时或结果不明时不会自动重放本次任务，但下一次已配置的日程仍会正常运行。第三方服务规则、令牌有效期和上游兼容性不由本项目保证。当前正式版 `v1.7.0` 可能将成长中心部分失败掩盖为整体成功；门户只依据上游公开的退出码和结果字段判断，不能恢复被上游丢弃的细节。后续正式版本修复后可通过子模块更新继承，不在本仓库私自打补丁。

## 凭据格式

门户支持粘贴 JSON，或在「添加账户 → 上传原始 .info / .json 凭据文件」中直接选择 CodeBuddy 的 Windows `Tencent-Cloud.coding-copilot.info`，无需重命名、手工删字段或复制 Token。按内容而非文件名解析 UTF-8 JSON（支持 BOM），最大 64 KiB；仅使用当前顶层 `auth` / `account`，不自动批量导入 `accounts` / `allAccounts`。支持扁平会话或 CodeBuddy 原生结构：

```json
{
  "accessToken": "YOUR_ACCESS_TOKEN",
  "uid": "YOUR_UID"
}
```

```json
{
  "auth": {"accessToken": "YOUR_ACCESS_TOKEN"},
  "account": {"uid": "YOUR_UID"}
}
```

以上都是占位符，不是可用凭据；`xx` 等脱敏值无法还原，会被明确拒绝。仅保留 `auth.accessToken`、官方 `auth.endpoint`、`account.uid` 及可选 `auth.domain`、`account.enterpriseId`；固定使用官方 HTTPS 地址，不接受任意 endpoint。`refreshToken`、昵称、手机号及其他账户列表不会保存，原始文件不落盘。**只有 accessToken 为明文字符串才能直接导入**；`$wbEncrypted` / `envelope` 等客户端加密 token 不支持，服务器会明确拒绝，不保存密文对象，也不尝试解密或猜测密钥。门户不会替你获取账号密码、绕过登录或刷新令牌。当前为 HTTP 时页面会提示传输风险，建议仅通过 HTTPS 或可信加密通道上传。

账户编辑页不会回显已保存的 token；留空表示保留原凭据。表单验证失败时，提交的凭据也不会回显。可以用「在线认证检查」确认服务端是否接受当前会话；本地格式检查不等同于在线认证成功。

## 安全边界

- 账户备注及会话以 **AES-256-GCM** 加密入库，随机 nonce，且绑定账户 UUID，不能将密文跨账户搬运。
- 密钥环、Django secret 和数据库密码通过仓库外的文件挂载。镜像、Git、Release 和 CI 产物不应包含真实凭据。
- 管理员只通过交互式 `bootstrap_admin` 初始化；密码不在源码、命令行参数或部署示例中。使用 Argon2 哈希。
- 使用 Django session、CSRF、防缓存页面、同源 CSP、安全 Cookie 和 HTTPS；未提供公开注册入口。
- 登录失败按数据库记录的来源限速：15 分钟内 5 次失败后阻止 15 分钟。反向代理后来源可能合并，因此这是保守的共享限流。**目前没有 TOTP/MFA**；建议限制管理员网络访问范围，使用强唯一密码。
- 工作进程使用独立、最小化环境，只有当前任务的会话会短暂写到容器 tmpfs 私有目录；完成或异常后清理。不继承数据库密钥路径，不把上游原始输出或 traceback 写入任务记录。
- Web/worker 非 root、只读根文件系统、无 Docker socket；数据库不对公网发布。主机更新器属于另一条受信任管理边界，需要 Docker 管理权限，不能由 Web 请求触发。
- 子进程和 worker 共享容器及 UID，最小环境不是强隔离沙箱；受信任的上游脚本仍可能读取同容器挂载的秘密文件。因此必须固定、审查上游版本，不应执行任意第三方脚本。
- 有主机 root、数据库和外部密钥的人仍能取得账户会话。加密不能代替服务器安全、密钥备份及权限管理。

**不要**把真实 WorkBuddy 登录文件、令牌、管理员密码、SSH 私钥、数据库转储、真实 `.env` 或密钥环提交到仓库。将仓库改为公开前应重新检查整个提交历史及 Release 产物。示例文件只使用占位符，测试只使用明确的 synthetic 数据。

## 明文 HTTP 模式（默认关闭，不推荐）

默认部署由 Caddy 终止 TLS，强制 HTTPS 跳转、`Secure` Cookie 和 HSTS。仅当确实需要「公网 IP + 独立端口、直接以 HTTP 访问」时，才设置 `AUTOBUDDY_ALLOW_HTTP=1`（默认 `0`）。**该开关本身不会把默认 Caddy 拓扑改成 HTTP**：明文访问需要另行部署一个受控的独立反向代理（配置见[部署运行手册](deployment/README.md)），应用端口仍只能绑定回环或内部地址，不得直接暴露到公网。开关只放宽传输层，不会改变单管理员登录、CSRF 校验、会话超时、Cookie HttpOnly/SameSite 设置和登录失败限速。

明文 HTTP 下管理员账号、密码和会话 Cookie 全程未加密，链路上的任何人都能读取或篡改，因此默认关闭，推荐 HTTPS。开启时 `ALLOWED_HOSTS` 必须包含该访问主机（不含端口）；反向代理若只转发不含端口的 Host，浏览器 `Origin` 仍带端口，因此 `CSRF_TRUSTED_ORIGINS` 必须显式包含完整的 `http://主机:端口` 来源。`DJANGO_DEBUG` 仍必须为 `false`：生产调试模式依旧被拒绝，不能用它替代该开关。此前访问过同一主机名并收到 HSTS 的浏览器会继续强制或升级到 HTTPS（HSTS 不适用于 IP 地址），所以这类主机名只适合临时使用；`manage.py check --deploy` 在该模式下报告传输安全警告属于预期结果。

浏览器的 Cookie 只按主机隔离、不区分端口，同一公网 IP 不同端口的多个门户会互相覆盖默认的 `sessionid` / `csrftoken`。因此本项目的会话与 CSRF Cookie 在所有模式下都使用独立名称 `autobuddy_session` / `autobuddy_csrftoken`，与其他门户共存时不会串号。

## 部署与运行

完整的首次安装、域名/TLS、管理员初始化、备份、升级和回滚步骤见 [部署运行手册](deployment/README.md)；发布前检查 [Release 清单](deployment/RELEASE_CHECKLIST.md)。当前代码不预置真实域名、管理员密码或服务器 SSH 信息；提供服务器连接信息后再执行实际部署。

基础操作（在已经配置外部秘密文件的部署目录执行）：

```sh
git clone --recurse-submodules https://github.com/Minhyuin/auto-buddy.git
cd auto-buddy
# 先按部署手册配置 runtime.env 与仓库外的 secrets，再启动。
docker compose --env-file deployment/runtime.env up -d --build
docker compose --env-file deployment/runtime.env exec web python manage.py bootstrap_admin
```

管理员也可在首次进入网页时交互创建：此模式默认关闭，仅支持通过 SSH 隧道访问的回环 HTTPS 部署，由主机管理员短时开启 `INITIAL_SETUP_DEADLINE`（最长 30 分钟窗口）。创建成功或已有任何用户后立即关闭，不提供公开注册；完整启用和关闭步骤见部署手册。密码仅在受保护的网页中输入，不写入命令、配置或仓库。

维护命令通过容器运行，不需要在主机另装 Python 依赖：

```sh
# 更新前暂停产生/领取新任务；当前任务仍会完成。
docker compose --env-file deployment/runtime.env exec -T worker python manage.py worker_control pause
docker compose --env-file deployment/runtime.env exec -T worker python manage.py worker_control drained
# 完成维护后恢复。
docker compose --env-file deployment/runtime.env exec -T worker python manage.py worker_control resume
```

`drained` 在仍有执行中任务时返回非零；不要对结果不明的写操作盲目重试。更新前需要备份 PostgreSQL，并单独保护、备份密钥；丢失密钥无法从数据库密文恢复账户。失败回退必须考虑数据库迁移是否向后兼容，不能仅切换旧镜像就假定安全。

## 本地测试（不会联系真实 WorkBuddy）

Python 3.12+：

```sh
python -m venv .venv
# 激活该虚拟环境，安装运行及开发依赖。
python -m pip install -r requirements.txt -r tests_devops/requirements.txt
python -m pip install ruff
# Linux/macOS
export AUTOBUDDY_TESTING=1
# PowerShell 改为：$env:AUTOBUDDY_TESTING='1'
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python -m ruff check autobuddy portal tests scripts tests_devops
```

`AUTOBUDDY_TESTING=1` 只用于测试：使用固定的 synthetic 密钥和内存 SQLite，关闭生产 HTTPS 跳转。**不得用于部署或添加真实账户**。CI 可通过 `TEST_DATABASE_HOST` 等变量改为 PostgreSQL 测试。进程集成测试仅执行临时生成的假上游脚本，验证超时、最小环境、输出脱敏和临时文件清理，不消耗真实积分，也不验证真实 WorkBuddy 令牌。

## 上游与许可

上游子模块保持自己的 [MIT 许可](workbuddy-auto-signin/LICENSE) 与版权声明；上游版本更新以正式 Release 对应的固定 commit 为边界，不直接追踪默认分支 HEAD。本项目自身的许可证尚待仓库所有者选择，不将上游的 MIT 许可自动扩大为对本项目新增代码的授权声明。
