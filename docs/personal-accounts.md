# 个人账号与实时资料

账号功能默认关闭。开启后，每个 API 使用者通过自己的 JM 账号登录，得到本服务的随机 Bearer Token。账号密码、Cookie 和登录资料使用 AES-GCM 加密保存到本地；Token 仅保存摘要。加密密钥必须持久保存，丢失后无法恢复账号会话。当前只支持单进程部署。

## 启用

在项目根目录执行一次：

```bash
python3 scripts/init-auth.py
```

脚本生成 32 字节随机密钥和状态目录，拒绝覆盖已有密钥，不打印密钥内容。两者均已加入 Git 和 Docker 构建忽略列表。备份时保存密钥与加密状态，限制访问权限；不要把真实凭证提交到配置文件。

本地运行配置：

```dotenv
JMAPI_AUTH_ENABLED=true
JMAPI_AUTH_KEY_FILE=./secrets/session.key
JMAPI_AUTH_STATE_DIR=./state/sessions
```

Docker 使用覆盖文件，并以刚才生成文件的非 root 用户身份运行：

```bash
sudo env JMAPI_UID="$(id -u)" JMAPI_GID="$(id -g)" \
  docker compose -f compose.yaml -f compose.auth.yaml up -d --build
```

密钥只读挂载，状态目录可写挂载；不需要重新生成密钥。启用时密钥缺失、格式错误、会话无法解密或目录不可写会明确报错。三个媒体缓存卷继续使用原有配置。不要运行多个 worker 共同写这个状态目录。

## 登录和刷新

`POST /auth/login` 的 JSON 为 `{"username":"账号","password":"密码"}`。成功返回 `data.access_token`、`token_type`、`expires_at`、`profile`、`fetched_at`。通过 HTTPS 或可信本地连接传输账号密码，在客户端保护 Token。

后续请求使用 `Authorization: Bearer <access_token>`，不要把 Token 放进 URL。现有查询、整本 PDF、路径和分片接口都会选择该个人账号；不带 Token 时使用真正的匿名客户端。开启个人账号后，匿名客户端不继承 option.yml 的服务账号、Cookie 或初始化插件。个人运行时使用 API 客户端，不执行服务器的 after_init 插件；其他下载阶段插件仍由服务器配置管理。

**每次 `GET /auth/me` 都会使用加密保存的账号密码，重新向 JM 登录一次，返回这次登录的最新资料。** 同账号请求串行执行，各次读取仍分别登录，不用旧资料冒充成功结果。登录失败时返回错误，不返回上次资料。并行请求的网络等待受全局登录额度约束；默认同时登录 2 个、等待队列 32，单次上游操作时限 30 秒。

资料白名单包含 uid、username、email、emailverified、photo、fname、gender、coin、album_favorites、level_name、level、nextLevelExp、exp、expPercent、badges、album_favorites_max。未返回的字段为 null，排除密码、Cookie、`s` 等会话字段。`fetched_at` 为本次登录成功时间。

普通查询或下载收到明确的上游 401（包括 API JSON 的 code=401）时，自动重新登录并最多重新执行一次。相同会话的并发旧请求可复用已完成的重新登录；一般网络失败、403、作品不存在不会触发重新登录。下载子任务中的认证失败也会传播到该恢复流程。

`DELETE /auth/session` 撤销当前 Token 并删除其持久文件，取消该会话所属任务，执行器实际退出后才释放资源。已经开始的文件响应允许完成。该接口清除本服务会话，不承诺撤销 JM 服务端其他会话。Token 默认绝对有效期 7 天、空闲有效期 24 小时；JM 会话自动恢复不会延长本服务 Token 的绝对有效期。

## 收藏和签到

| 接口 | 行为 |
| --- | --- |
| `GET /me/favorites?page=1&folder_id=0` | 实时读取收藏及文件夹列表，合并同一会话的相同在途请求 |
| `POST /me/favorites/{album_id}/toggle` | 切换收藏，返回实际上游执行的 add/remove |
| `GET /me/daily` | 实时读取签到状态和日历，不执行签到 |
| `POST /me/daily/check-in` | 主动签到，返回是否今日已经签到 |

写接口必须携带 `Idempotency-Key`，长度为 1–128 字符。同账号操作串行执行，操作记录加密持久化，24 小时内相同键和参数返回原结果，不重复写入；不同参数返回 409。结果记录独立于 Token，退出再登录仍可复用。单账号保留最多 1024 条未过期记录。

写请求关闭自动传输重试。若请求已发送后发生网络错误、超时或退出，结果可能未知，相同幂等键返回 409，应先查询当前状态。明确 401 拒绝时才清除未执行记录并允许重新登录后尝试一次。客户端不要在结果未知时换一个新键盲目重试。

## 缓存与运行限制

按照本项目选定策略，原图、PDF、分片和单页编码完全共享：某个账号下载成功后，匿名请求也能直接读取缓存，不做上游权限复核。共享缓存命中不会触发重新登录；`/auth/me` 除外，它每次都登录。

个人资料、收藏、签到及查询客户端分别绑定身份；查询缓存和在途任务包含身份/配置代际。下载在途按身份合并，同一作品共享写锁；匿名下载失败不成为其他账号的失败缓存。

默认最多 256 个有效 Token，最多 32 个常驻个人运行时，其余按需恢复。所有账号共用已有下载、章节、图片、查询和编码线程池；登录使用独立的 2 线程池。当前尚未加入每会话下载/查询公平调度，以及原生异步查询对照，这两项仍属于后续上游优化批次。

HTTP 401 表示本服务会话或上游登录被拒绝；网络错误通常为 502/504，容量或持久化不可用为 503；登录入口按来源地址每分钟限 10 次尝试，超限为 429。个人响应设置 `Cache-Control: no-store`。验证错误不回显输入，个人上游异常不透传原始响应。
