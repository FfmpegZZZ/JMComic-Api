# JMComic API

本仓库基于 [LingLambda/JMComic-Api](https://github.com/LingLambda/JMComic-Api) 修改，提供一个用于与禁漫天堂（JMComic）交互的 Web API 服务。

## v2.1 更新

升级 jmcomic 至 2.7.11，加入任务合并与有界并发、可取消下载、独立进程构建 PDF、v2 缓存、流式 Base64 和可选多用户登录。继续采用单实例、单进程、本地文件存储；原有接口与默认返回方式保留，旧文件按需迁移。

Python 3.12 / 3.13 各 172 项测试通过；已在 ARM64 机器完成真实上游和账号登录验证。实测收益与首次构建的性能取舍见 [ARM 报告](docs/arm-benchmark-2026-11-06.md)，配置和迁移要求见 [优化说明](docs/optimization.md)。

## 2026 重构 (v2.0)

整套底层重写，从 Flask + waitress + PyPDF2 迁移到 **FastAPI + uvicorn + pypdf**，并升级 jmcomic 到 `2.6.18`。

新结构：

```
src/jmcomic_api/
    app.py              # FastAPI 应用 + lifespan
    settings.py         # pydantic-settings（环境变量驱动）
    routers/            # pdf / catalog / health
    services/           # jm_client adapter + album service + pdf builder
    concurrency/        # asyncio.Lock keyed dict（替换原 KeyedTaskQueue）
    schemas/            # 请求/响应模型
tests/                  # pytest（含 e2e）
Dockerfile              # 多阶段，COPY 本地源码（不再 git clone 上游）
compose.yaml
```

### Breaking changes vs 1.x

| 项 | 旧 | 新 |
|---|---|---|
| 入口 | `python main.py` | `python -m jmcomic_api` 或 `uvicorn jmcomic_api.app:app` |
| 配置文件 | 改 `app/core/config.py` 源码 | 环境变量 / `.env`（前缀 `JMAPI_`） |
| `option.yml` 热重载 | watchdog 文件监听（双触发问题） | `kill -HUP <pid>` 信号触发 |
| PDF 加密引擎 | PyPDF2 | pypdf（版本以 `uv.lock` 为准；旧文件保留，按需生成 v2 缓存） |
| `JM`-前缀 ID | 缓存被破坏（bug） | `JM12345` / `12345` 同一缓存 |
| 错误响应 | 笼统 500 | 按 `JmcomicException` 子类映射 404 / 502 / 503 |
| 文档 | `/docs` 跳转 apifox | FastAPI 自动 `/docs` Swagger UI |
| 兼容 shim | `album_service.py` / `config.py` 在根目录 | 已删除，外部 import 需改为 `from jmcomic_api...` |

## 注意

本项目主要使用 jmcomic 移动端 API，对 IP 要求相对较低。

## 使用方法

### 直接运行（推荐使用 [uv](https://docs.astral.sh/uv/)）

```bash
# 安装 uv（首次）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 按锁文件创建 venv 并装依赖（含本地测试工具）
uv sync --python 3.12 --extra dev --locked

# （可选）复制 .env 配置
cp .env.example .env

# 启动
uv run uvicorn jmcomic_api.app:app --host 0.0.0.0 --port 8699
# 或：python -m jmcomic_api
```

启动后访问 `http://localhost:8699/docs` 查看自动生成的 Swagger UI。

本地验证（Python 3.12 / 3.13）：

```bash
uv run --locked pytest tests/ -W error
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
```

测试包含真实图片转 PDF、JPEG 压缩、加解密和分片验证；搜索、下载等上游网络请求使用模拟数据，不依赖 JM 站点可用性。2026-11-05 更新的核心依赖为 `jmcomic 2.7.11`、`FastAPI 0.162.0`、`pypdf 6.21.4`、`Pillow 13.0.0`，完整版本见 `uv.lock`。

### 使用 Docker

```bash
# 自构建（推荐，确保镜像内容跟仓库代码一致）
docker compose up -d --build

# 或拉取预构建（如果 CI 还在推 orwellz/jmcomic-api 这个 image）
docker run -d --name jmcomic-api -p 8699:8699 orwellz/jmcomic-api:latest
```

热重载 jmcomic 配置（不重启容器）：

```bash
docker kill -s HUP jmcomic-api
```

## API 接口文档

以下是 JMComic API 提供的接口详情：

### 1. 获取 PDF 文件

*   **路径:** `/get_pdf/<jm_album_id>`
*   **方法:** `GET`
*   **功能:** 根据 ID 下载对应的漫画，生成并返回 PDF 文件。
*   **路径参数:**
    *   `jm_album_id`: 车牌号。
*   **查询参数:**
    *   `passwd` (可选，默认加密): 控制 PDF 是否加密。设置为 `'false'` 或 `'0'` 表示不加密。
    *   `Titletype` (可选, 默认 2): 控制生成的 PDF 文件名的格式:
        *   `0`: `<jm_album_id>.pdf`
        *   `1`: `<相册标题>.pdf`
        *   `2` (或其他值): `[<jm_album_id>] <相册标题>.pdf`
    *   `pdf` (可选, 字符串, 默认 'false'): 控制返回类型。如果设置为 `'true'`，则直接以 `application/pdf` 类型返回文件供下载。
*   **成功返回 (pdf='false'):**
    ```json
    {
        "success": true,
        "message": "PDF 获取成功",
        "name": "生成的PDF文件名.pdf",
        "data": "<Base64编码的PDF内容>"
    }
    ```
*   **成功返回 （pdf=true）:**
    *   HTTP 状态码 `200`
    *   `Content-Type: application/pdf`
    *   文件内容作为响应体。
*   **失败返回:**
    ```json
    {
        "success": false,
        "message": "错误信息"
    }
    ```
*   **示例:**
    *   获取 Base64 编码的 PDF: `GET /get_pdf/12345`
    *   直接下载 PDF 文件: `GET /get_pdf/12345?pdf=true`
    *   获取不加密的 PDF: `GET /get_pdf/12345?passwd=false`
    *   使用标题作为文件名: `GET /get_pdf/12345?Titletype=1`

### 2. 获取 PDF 文件路径

*   **路径:** `/get_pdf_path/<jm_album_id>`
*   **方法:** `GET`
*   **功能:** 与 `/get_pdf` 类似，会触发下载和生成 PDF（如果需要），但最终返回 PDF 文件在服务器上的绝对路径。
*   **路径参数:**
    *   `jm_album_id`: 禁漫天堂的相册 ID。
*   **查询参数:**
    *   `passwd` (可选, 字符串, 默认 'true'): 同 `/get_pdf`。
    *   `Titletype` (可选, 整数, 默认 2): 同 `/get_pdf`。
*   **成功返回:**
    ```json
    {
        "success": true,
        "message": "PDF 获取成功",
        "data": "/path/to/server/pdf/v2/12345/<内容键>.pdf",
        "name": "[12345] 标题.pdf"
    }
    ```
*   **失败返回:**
    ```json
    {
        "success": false,
        "message": "错误信息"
    }
    ```
*   **示例:** `GET /get_pdf_path/12345`

### 3. 搜索漫画

*   **路径:** `/search`
*   **方法:** `GET`
*   **功能:** 根据提供的关键词在禁漫天堂网站上搜索漫画。
*   **查询参数:**
    *   `query` (必需, 字符串): 要搜索的关键词。
    *   `page` (可选, 整数, 默认 1): 搜索结果的页码。
*   **成功返回:**
    ```json
    {
        "success": true,
        "message": "Search successful",
        "data": {
            "results": [
                {"id": "album_id_1", "title": "title_1"},
                {"id": "album_id_2", "title": "title_2"}
                // ...
            ],
            "current_page": 1,
            "has_next_page": true // 或 false
        }
    }
    ```
*   **失败返回:**
    ```json
    {
        "success": false,
        "message": "错误信息 (例如 'Missing 'query' parameter')"
    }
    ```
*   **示例:** `GET /search?query=dingyi&page=2`

### 4. 获取详情

*   **路径:** `/album/<jm_album_id>`
*   **方法:** `GET`
*   **功能:** 根据jm_album_id获取tag。
*   **路径参数:**
    *   `jm_album_id`: 车牌号。
*   **成功返回:**
    ```json
    {
        "success": true,
        "message": "Album details retrieved",
        "data": {
            "id": "12345",
            "title": "相册标题",
            "tags": ["tag1", "tag2", ...]
        }
    }
    ```
*   **失败返回 (例如 404 Not Found):**
    ```json
    {
        "success": false,
        "message": "Album with ID '12345' not found..."
    }
    ```
*   **示例:** `GET /album/12345`

### 5. 按分类浏览

*   **路径:** `/categories`
*   **方法:** `GET`
*   **功能:** 根据分类、时间范围和排序方式浏览禁漫天堂的漫画列表。
*   **查询参数:**
    *   `page` (可选, 整数, 默认 1): 结果页码。
    *   `time` (可选, 字符串, 默认 'all'): 时间范围。可用值: `'today'`, `'week'`, `'month'`, `'all'`, `'t'`, `'w'`, `'m'`, `'a'`。
    *   `category` (可选, 字符串, 默认 'all'): 漫画分类。可用值: `'doujin'`, `'single'`, `'short'`, `'another'`, `'hanman'`, `'meiman'`, `'doujin_cosplay'`, `'cosplay'`, `'3d'`, `'english_site'`, `'all'`。
    *   `order_by` (可选, 字符串, 默认 'latest'): 排序方式。可用值: `'latest'`, `'view'`, `'picture'`, `'like'`, `'month_rank'`, `'week_rank'`, `'day_rank'`。
*   **成功返回:**
    ```json
    {
        "success": true,
        "message": "Categories retrieved successfully",
        "data": {
            "results": [
                {"id": "album_id_3", "title": "漫画标题3"},
                {"id": "album_id_4", "title": "漫画标题4"}
                // ...
            ],
            "current_page": 3,
            "has_next_page": true, // 或 false
            "params_used": { // 显示实际使用的参数值
                "time": "all",
                "category": "hanman",
                "order_by": "view"
            }
        }
    }
    ```
*   **失败返回:**
    ```json
    {
        "success": false,
        "message": "错误信息"
    }
    ```
*   **示例:** `GET /categories?category=hanman&order_by=view&page=3`

本轮任务、缓存、流式传输及新配置详见 [优化与迁移说明](docs/optimization.md)。Docker 本地测试及原版对比见 [运行步骤](docs/docker-comparison.md)。旧 PDF 按需迁移并默认保留，不需要清空缓存目录。

上游请求、jmcomic 原生异步能力及后续实施顺序见 [上游网络优化评估](docs/upstream-review.md)。

多用户登录、加密保存密码、每次读取个人资料重新登录，以及收藏和签到接口见 [个人账号说明](docs/personal-accounts.md)。功能默认关闭，按说明生成持久密钥后启用。
