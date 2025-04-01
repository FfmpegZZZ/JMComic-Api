# JMComic API

本仓库基于 [LingLambda/JMComic-Api](https://github.com/LingLambda/JMComic-Api) 修改，提供一个用于与禁漫天堂（JMComic）交互的 Web API 服务。

## 使用方法

你可以选择直接运行源代码或使用 Docker 镜像。

### 直接运行

1.  **下载源码包并解压**，然后进入项目根目录。
2.  **创建并激活 Python 虚拟环境**:
    *   创建: `python -m venv .venv`
    *   激活 (Windows): `.\.venv\Scripts\activate`
    *   激活 (macOS/Linux): `source .venv/bin/activate`
3.  **安装依赖**:
    ```bash
    pip install -r requirements.txt
    ```
4.  **(可选) 配置**: 编辑 `option.yml` 文件以配置 JMComic 客户端选项和 API 服务设置（如主机和端口）。默认服务运行在 `0.0.0.0:8699`。
5.  **运行**:
    ```bash
    python main.py
    ```
    服务将在 `option.yml` 中配置的地址和端口启动。

### 使用 Docker (推荐)

我们提供了预构建的 Docker 镜像 `orwellz/jmcomic-api`

1.  **拉取镜像**:
    ```bash
    docker pull orwellz/jmcomic-api:latest
    ```
2.  **运行容器**:
    ```bash
    # 运行在后台，将容器的 8699 端口映射到宿主机的 8699 端口
    docker run -d --name jmcomic-api -p 8699:8699 orwellz/jmcomic-api:develop

## API 接口文档

以下是 JMComic API 提供的接口详情：

### 1. 获取 PDF 分片信息

*   **路径:** `/pdf/info/{jm_album_id}`
*   **方法:** `GET`
*   **功能:** 获取指定相册的 PDF 分片信息。API 会自动下载相册图片（如果本地不存在）。分片大小固定为 100 页。
*   **路径参数:**
    *   `jm_album_id`: 禁漫天堂的相册 ID (车牌号)。
*   **成功返回:**
    ```json
    {
        "success": true,
        "message": "PDF shard info retrieved successfully",
        "data": {
            "jm_album_id": "12345",
            "title": "相册标题",
            "total_pages": 567,
            "shard_size": 100, // 固定值
            "shards": [
                {"shard_index": 1, "start_page": 1, "end_page": 100},
                {"shard_index": 2, "start_page": 101, "end_page": 200},
                // ...
                {"shard_index": 6, "start_page": 501, "end_page": 567}
            ]
        }
    }
    ```
*   **失败返回 (例如 404 Not Found 或 502 上游错误):**
    ```json
    {
        "detail": "Album 12345 not found online or access error." // 或其他错误信息
    }
    ```
*   **示例:** `GET /pdf/info/12345`

### 2. 获取 PDF 分片文件

*   **路径:** `/pdf/shard/{jm_album_id}/{shard_index}`
*   **方法:** `GET`
*   **功能:** 获取指定相册的特定 PDF 分片（固定 100 页）。如果缓存不存在，则会实时生成。
*   **路径参数:**
    *   `jm_album_id`: 禁漫天堂的相册 ID。
    *   `shard_index`: 要获取的分片索引（从 1 开始）。
*   **查询参数:**
    *   `pdf` (可选, 布尔值, 默认 `false`): 如果设置为 `true`，则直接返回 `application/pdf` 文件供下载；否则返回包含 Base64 编码内容的 JSON。
*   **成功返回 (pdf=false):**
    ```json
    {
        "title": "相册标题",
        "success": true,
        "message": "PDF shard generated and encoded successfully.", // 或 "PDF shard found in cache and encoded."
        "shard_index": 2,
        "data": "<Base64编码的PDF分片内容>"
    }
    ```
*   **成功返回 (pdf=true):**
    *   HTTP 状态码 `200`
    *   `Content-Type: application/pdf`
    *   PDF 文件内容作为响应体。
*   **失败返回 (例如 404 Not Found 或 500 Server Error):**
    ```json
    {
        "detail": "Invalid shard index 7. Valid range is 1 to 6 for shard size 100." // 或其他错误信息
    }
    ```
*   **示例:**
    *   获取第 2 个分片的 Base64 JSON: `GET /pdf/shard/12345/2`
    *   直接下载第 3 个分片的 PDF 文件: `GET /pdf/shard/12345/3?pdf=true`

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
