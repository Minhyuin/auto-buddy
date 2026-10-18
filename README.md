# AutoBuddy

## 快速开始

准备一台 **Ubuntu 22.04 x86_64** 服务器，安装 Git、Python 3、OpenSSL、Docker Engine 和 Docker Compose；将域名解析到服务器，开放 **80/443** 端口。以下为源码构建部署，以 root 执行。

### 1. 安装

```bash
git clone --recurse-submodules --branch v0.4.1 https://github.com/Minhyuin/auto-buddy.git /opt/auto-buddy-src
cd /opt/auto-buddy-src
docker build --build-arg APP_VERSION=v0.4.1 -t auto-buddy:local .
bash deployment/install.sh --generate-secrets --skip-systemd
```

### 2. 配置域名

编辑 `/etc/auto-buddy/runtime.env`，修改以下三项，其余保留默认值：

```dotenv
APP_DOMAIN=buddy.example.com
ALLOWED_HOSTS=buddy.example.com,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://buddy.example.com
```

### 3. 启动并创建管理员

```bash
cd /opt/auto-buddy
export SECRETS_DIR=/etc/auto-buddy/secrets RUNTIME_ENV_FILE=/etc/auto-buddy/runtime.env
export APP_IMAGE=auto-buddy:local APP_VERSION=v0.4.1
docker compose --env-file "$RUNTIME_ENV_FILE" up -d --no-build --wait
docker compose --env-file "$RUNTIME_ENV_FILE" exec web python manage.py bootstrap_admin
```

按提示设置自己的管理员账号和密码，访问 **`https://buddy.example.com`** 登录，在「添加账户」上传 CodeBuddy 的原始 `.info` 文件（须包含有效的明文 `accessToken`）。

不要提交真实凭据、密钥或运行配置。签名镜像部署、自动更新及备份方法见 [部署手册](deployment/README.md)。