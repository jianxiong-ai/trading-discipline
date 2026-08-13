# 阿里云 ACR 部署与日常更新

本项目的生产部署使用阿里云容器镜像服务（ACR）：ACR 从 GitHub `main`
构建 `linux/amd64` 镜像，ECS 只拉取并运行完成的镜像，不在服务器构建代码。

```text
GitHub main
  -> ACR 构建 trading-discipline:latest
  -> ECS 拉取镜像
  -> monitor + web 容器共享服务器上的 data/
```

## 服务器上的持久化数据

ECS 目录固定为 `/opt/discipline-bot`。以下内容只保存在服务器，更新镜像
不会覆盖它们：

- `data/`：SQLite 台账、工作区、运行状态、审计日志和证据缓存。
- `config.yaml`：策略配置。
- `.env`：飞书、LLM、通知加密密钥及 `ACR_IMAGE`。

不要执行 `docker compose down -v`。本项目使用主机目录挂载而非 Docker
命名卷，但 `data/` 仍是账户事实和运行状态的唯一来源。

## 一次性切换到 ACR

### 1. 创建 ACR 仓库和构建规则

在阿里云控制台进入 **容器镜像服务 ACR → 个人版实例 → 镜像仓库**：

1. 在与 ECS 相同地域的命名空间中创建仓库 `trading-discipline`。
2. 设置代码源为 GitHub 仓库 `jianxiong-ai/trading-discipline`。
3. 设置构建分支为 `main`，Dockerfile 路径为 `Dockerfile`，构建上下文为仓库根目录。
4. 为 `main` 推送配置自动构建，生成镜像标签 `latest`。
5. 首次构建完成后，记下仓库完整镜像地址，例如：

   ```text
   registry.cn-hangzhou.aliyuncs.com/your-namespace/trading-discipline:latest
   ```

### 2. 登录 ECS 并配置镜像地址

在 ECS 上备份持久化数据：

```bash
cd /opt/discipline-bot
tar -czf discipline-data-backup-$(date +%F).tgz data config.yaml .env
```

编辑 `.env`，追加 ACR 镜像地址：

```bash
ACR_IMAGE=registry.cn-hangzhou.aliyuncs.com/your-namespace/trading-discipline:latest
```

在 ACR 仓库页面获取登录命令和访问凭证后，在 ECS 执行该登录命令。登录成功后，
将本仓库的 `docker-compose.acr.yml` 上传到：

```text
/opt/discipline-bot/docker-compose.acr.yml
```

### 3. 首次从 ACR 启动

仍在 ECS 项目目录执行：

```bash
docker compose -f docker-compose.acr.yml pull
docker compose -f docker-compose.acr.yml up -d
docker compose -f docker-compose.acr.yml ps
```

`monitor` 和 `web` 都应显示为运行中。网页仍通过：

```text
http://ECS公网IP:8787
```

验证：

```bash
curl http://127.0.0.1:8787/health
docker compose -f docker-compose.acr.yml exec monitor python -m astock_bot.main health
```

## 日常更新

### 1. 推送代码到 GitHub main

在本机：

```bash
cd /Users/jasonjiang/Developer/a-stock-discipline-bot
git switch main
git pull --ff-only origin main
git add <本次修改的文件>
git commit -m "说明本次修改"
git push origin main
```

### 2. 等待 ACR 构建成功

在 ACR 仓库的构建记录中确认最新 `latest` 构建状态为成功。

### 3. 在 ECS 拉取并重启

```bash
cd /opt/discipline-bot
docker compose -f docker-compose.acr.yml pull
docker compose -f docker-compose.acr.yml up -d
docker compose -f docker-compose.acr.yml ps
```

不要添加 `--build`：镜像已经由 ACR 构建。不要使用 `down -v`。
