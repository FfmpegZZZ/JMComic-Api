# Docker 本地验证与原版对比

基线固定为 `feb1cc6`，使用该提交的代码和锁文件；优化版使用当前工作目录，保留已有依赖升级。两版使用相同 Python 3.12、uv 0.13.7、2 CPU、2 GiB 内存以及同一份固定离线图片。对比反映依赖与代码的总体变化。

在项目目录运行以下一条命令，先构建两份本地镜像，再准备离线数据并启动。任何一步失败都会停止，不会继续拉取缺失的本地镜像：

```sh
sh scripts/start-comparison.sh
```

如果当前用户访问 `/var/run/docker.sock` 被拒绝，使用 `sudo sh scripts/start-comparison.sh`。构建文件兼容当前机器上的旧版 Docker 构建器和 BuildKit，不要求安装 buildx；旧构建器的弃用提示不影响本轮构建。

`jmcomic-api:bench-baseline` 和 `jmcomic-api:bench-optimized` 是本地构建标签，不是已发布的仓库镜像。出现 `pull access denied` 或 `No such image` 表示前面的本地构建没有完成，不需要为这两个标签执行 `docker login`；重新运行上述脚本，并查看最早的构建错误。

如需单独构建并运行 Python 测试镜像和生产镜像：

```sh
sh scripts/build-comparison.sh
# 同时构建原版、优化版、Python 3.12/3.13 测试镜像和生产镜像。
docker run --rm --network none jmcomic-api:test-py312
docker run --rm --network none jmcomic-api:test-py313
```

Compose 已补齐两个版本的构建上下文，并禁止拉取这两个本地标签。也可以在基线源码已准备好后用 `docker compose -f compose.benchmark.yaml build baseline optimized` 单独重建；首次运行推荐使用上面的一步启动脚本，它会自动从 Git 准备基线源码。

容器启动后通知当前对话继续测试，或自行运行：

```sh
python3 scripts/benchmark/compare.py --output benchmark-results
# 使用项目环境中的 Pillow / pypdf 验证内容和持续并发。
.venv/bin/python scripts/benchmark/verify_live.py --output benchmark-results
# 仅针对优化版：慢客户端、4 路上限、排队恢复、304/Range 和 30 秒发送超时。
.venv/bin/python scripts/benchmark/verify_network.py --output benchmark-results
```

只绑定本机回环地址：原版 `http://127.0.0.1:18700`，优化版 `http://127.0.0.1:18701`。测试夹具替换上游下载，使用实际公开 PDF 接口；不会连接 JM。夹具专用端点不进入生产镜像。

报告记录第一次构建、预热后五次构建的中位数/p95、PDF 热缓存、分片、服务器父子进程总 RSS、线程数、输出体积、10/100 MiB Base64 内存差和 20 个 10 MiB Base64 并发请求下健康接口 p95。100/500 页是固定的 512×768 JPEG 数据，不能代表所有漫画分辨率。RSS 由独立监控进程读取容器内 `/proc`，每约 10 ms 采样，因此服务事件循环阻塞时仍可采集；属于采样峰值，可能漏掉极短暂的峰值。报告不包含客户端内存。

比较 JPEG optimize 关闭选项时，重新创建优化版容器并重跑，使用不同报告目录；重置接口会移除测试 PDF，保留原图：

```sh
JMAPI_JPEG_OPTIMIZE=false docker compose -f compose.benchmark.yaml up -d --force-recreate optimized
python3 scripts/benchmark/compare.py --output benchmark-results/no-optimize
```

完成后停止对比容器（不删除数据）：

```sh
docker compose -f compose.benchmark.yaml down
```

生产服务使用 `docker compose up -d --build`。镜像默认 UID/GID 10001；绑定现有目录时先创建 `pdf`、`webp`、`pdf_cache` 并保证该 UID 可写，或设置 `JMAPI_UID` / `JMAPI_GID` 与目录所有者一致。例如 Linux 本机目录由当前用户所有时：

```sh
mkdir -p pdf webp pdf_cache
JMAPI_UID=$(id -u) JMAPI_GID=$(id -g) docker compose up -d --build
```

2026-11-05 已通过 HTTP 完成三轮用户启动的容器测试，详见 [实测报告](docker-benchmark-2026-11-05.md)。最新优化镜像的 500 页预热重建、热缓存与 Base64 更快，4 路发送、慢客户端隔离及排队恢复已在容器验证；首次构建仍较慢。以后只更新优化版可运行 `sudo docker compose -f compose.benchmark.yaml up -d --build --no-deps optimized`。代理工具不能自行执行需要 sudo 密码的 Docker 操作。
