from flask import Blueprint, jsonify, request, send_file, redirect, current_app
from jmcomic import JmSearchPage, JmAlbumDetail, JmCategoryPage, JmApiClient, JmModuleConfig
from jmcomic.jm_exception import JmcomicException
from jmcomic.jm_config import JmMagicConstants

from app.core.config import config
from app.services.album_service import get_album_pdf_path

bp = Blueprint('api', __name__)

# 修改产物命名规则
JmModuleConfig.AFIELD_ADVICE['jmbook'] = lambda album: f'[{album.id}]{album.title}'

def _get_state():
    """Helper to fetch dynamic state (opt, client) maintained by factory reloader."""
    state = current_app.config.get('state') or {}
    if 'opt' not in state or 'client' not in state:
        raise RuntimeError('Application state not initialized: opt/client missing')
    return state

TIME_MAP = {
    'today': JmMagicConstants.TIME_TODAY,
    'week': JmMagicConstants.TIME_WEEK,
    'month': JmMagicConstants.TIME_MONTH,
    'all': JmMagicConstants.TIME_ALL,
    't': JmMagicConstants.TIME_TODAY,
    'w': JmMagicConstants.TIME_WEEK,
    'm': JmMagicConstants.TIME_MONTH,
    'a': JmMagicConstants.TIME_ALL,
}
DEFAULT_TIME = JmMagicConstants.TIME_ALL

CATEGORY_MAP = {
    'all': JmMagicConstants.CATEGORY_ALL,
    'doujin': JmMagicConstants.CATEGORY_DOUJIN,
    'single': JmMagicConstants.CATEGORY_SINGLE,
    'short': JmMagicConstants.CATEGORY_SHORT,
    'another': JmMagicConstants.CATEGORY_ANOTHER,
    'hanman': JmMagicConstants.CATEGORY_HANMAN,
    'meiman': JmMagicConstants.CATEGORY_MEIMAN,
    'doujin_cosplay': JmMagicConstants.CATEGORY_DOUJIN_COSPLAY,
    'cosplay': JmMagicConstants.CATEGORY_DOUJIN_COSPLAY,
    '3d': JmMagicConstants.CATEGORY_3D,
    'english_site': JmMagicConstants.CATEGORY_ENGLISH_SITE,
}
DEFAULT_CATEGORY = JmMagicConstants.CATEGORY_ALL

ORDER_BY_MAP = {
    'latest': JmMagicConstants.ORDER_BY_LATEST,
    'view': JmMagicConstants.ORDER_BY_VIEW,
    'picture': JmMagicConstants.ORDER_BY_PICTURE,
    'like': JmMagicConstants.ORDER_BY_LIKE,
    'month_rank': JmMagicConstants.ORDER_MONTH_RANKING,
    'week_rank': JmMagicConstants.ORDER_WEEK_RANKING,
    'day_rank': JmMagicConstants.ORDER_DAY_RANKING,
}
DEFAULT_ORDER_BY = JmMagicConstants.ORDER_BY_LATEST


@bp.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


@bp.route('/get_pdf/<jm_album_id>', methods=['GET'])
def get_pdf(jm_album_id):
    passwd_str = request.args.get('passwd', 'true').lower()
    enable_pwd = passwd_str not in ('false', '0')
    try:
        # 恢复重构前默认 Titletype=2
        title_type = int(request.args.get('Titletype', 2))
    except ValueError:
        title_type = 2
    output_pdf_directly = request.args.get('pdf', 'false').lower() == 'true'
    state = _get_state()
    # 使用绝对路径，避免工作目录变化导致定位到 app/pdf
    path, name = get_album_pdf_path(
        jm_album_id,
        str(config.pdf_path),
        state['opt'],
        enable_pwd=enable_pwd,
        title_type=title_type,
    )
    if path is None:
        return jsonify({"success": False, "message": "PDF 文件不存在"}), 404

    if output_pdf_directly:
        try:
            return send_file(path, as_attachment=True, download_name=name)
        except Exception as e:
            return jsonify({"success": False, "message": f"发送 PDF 文件时出错: {e}"}), 500
    else:
        try:
            with open(path, "rb") as f:
                import base64
                encoded_pdf = base64.b64encode(f.read()).decode('utf-8')
            return jsonify({"success": True, "message": "PDF 获取成功", "name": name, "data": encoded_pdf})
        except Exception as e:
            return jsonify({"success": False, "message": f"读取或编码 PDF 文件时出错: {e}"}), 500


@bp.route('/get_pdf_path/<jm_album_id>', methods=['GET'])
def get_pdf_path(jm_album_id):
    import os
    passwd_str = request.args.get('passwd', 'true').lower()
    enable_pwd = passwd_str not in ('false', '0')
    try:
        title_type = int(request.args.get('Titletype', 2))
    except ValueError:
        title_type = 2
    state = _get_state()
    path, name = get_album_pdf_path(
        jm_album_id,
        str(config.pdf_path),
        state['opt'],
        enable_pwd=enable_pwd,
        title_type=title_type,
    )
    if path is None:
        return jsonify({"success": False, "message": "PDF 文件不存在"}), 404

    return jsonify({
        "success": True,
        "message": "PDF 获取成功",
        "data": os.path.abspath(path),
        "name": name,
    })


@bp.route('/search', methods=['GET'])
def search_comics():
    query = request.args.get('query')
    page_num = request.args.get('page', 1, type=int)

    if not query:
        return jsonify({"success": False, "message": "Missing 'query' parameter"}), 400

    try:
        state = _get_state()
        client: JmApiClient = state['client']
        page: JmSearchPage = client.search_site(search_query=query, page=page_num)
        results = [{"id": album_id, "title": title} for album_id, title in page]

        # simplistic next page check
        has_next_page = False
        try:
            next_page_check = client.search_site(search_query=query, page=page_num + 1)
            next(iter(next_page_check))
            has_next_page = True
        except StopIteration:
            has_next_page = False
        except JmcomicException:
            has_next_page = False

        return jsonify({
            "success": True,
            "message": "Search successful",
            "data": {
                "results": results,
                "current_page": page_num,
                "has_next_page": has_next_page,
            },
        })
    except JmcomicException as e:
        return jsonify({"success": False, "message": f"Jmcomic search error: {e}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500


@bp.route('/album/<jm_album_id>', methods=['GET'])
def get_album_details(jm_album_id):
    try:
        state = _get_state()
        client: JmApiClient = state['client']
        album: JmAlbumDetail = client.get_album_detail(jm_album_id)
        if not album:
            return jsonify({"success": False, "message": f"Album with ID '{jm_album_id}' not found."}), 404
        return jsonify({
            "success": True,
            "message": "Album details retrieved",
            "data": {"id": album.id, "title": album.title, "tags": album.tags},
        })
    except JmcomicException as e:
        return jsonify({"success": False, "message": f"Jmcomic error retrieving details: {e}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected server error occurred: {e}"}), 500


@bp.route('/categories', methods=['GET'])
def get_categories():
    page_num = request.args.get('page', 1, type=int)
    time_str = request.args.get('time', 'all').lower()
    category_str = request.args.get('category', 'all').lower()
    order_by_str = request.args.get('order_by', 'latest').lower()

    time_param = TIME_MAP.get(time_str, DEFAULT_TIME)
    category_param = CATEGORY_MAP.get(category_str, DEFAULT_CATEGORY)
    order_by_param = ORDER_BY_MAP.get(order_by_str, DEFAULT_ORDER_BY)

    try:
        state = _get_state()
        client: JmApiClient = state['client']
        page: JmCategoryPage = client.categories_filter(
            page=page_num,
            time=time_param,
            category=category_param,
            order_by=order_by_param,
        )

        results = [{"id": album_id, "title": title} for album_id, title in page]

        has_next_page = False
        try:
            next_page_check = client.categories_filter(
                page=page_num + 1,
                time=time_param,
                category=category_param,
                order_by=order_by_param,
            )
            next(iter(next_page_check))
            has_next_page = True
        except StopIteration:
            has_next_page = False
        except JmcomicException:
            has_next_page = False

        return jsonify({
            "success": True,
            "message": "Categories retrieved successfully",
            "data": {
                "results": results,
                "current_page": page_num,
                "has_next_page": has_next_page,
                "params_used": {
                    "time": time_param,
                    "category": category_param,
                    "order_by": order_by_param,
                },
            },
        })
    except JmcomicException as e:
        return jsonify({"success": False, "message": f"Jmcomic categories error: {e}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected server error occurred: {e}"}), 500


@bp.route('/docs')
def redirect_to_docs():
    return redirect("https://jm-api.apifox.cn")
