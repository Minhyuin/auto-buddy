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

门户支持粘贴 JSON 或上传 UTF-8 JSON，最大 64 KiB。支持扁平会话或原生 `auth` / `account` 结构：

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

以上都是占位符，不是可用凭据。仅保留必需字段及可选的 `domain`、`enterpriseId`；固定使用官方 HTTPS 地址，不接受任意 endpoint。**依赖桌面客户端解密的 accessToken 加密信封不能在无桌面服务器上直接使用**，应提供上游兼容的字符串会话。门户不会替你获取账号密码、绕过登录，也不会持久化 refreshToken。

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
