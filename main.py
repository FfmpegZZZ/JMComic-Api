import base64
from pathlib import Path

from jmcomic import create_option_by_file, JmApiClient, JmSearchPage, JmAlbumDetail, JmCategoryPage, JmModuleConfig
from jmcomic.jm_exception import JmcomicException 
from jmcomic.jm_config import JmMagicConstants 

from config import config

cfg = config()
host = cfg.host
port = cfg.port
optionFile = cfg.option_file
pdf_dir = cfg.pdf_dir

# 将下载产物命名规则改为[id]title，利于识别
JmModuleConfig.AFIELD_ADVICE['jmbook'] = lambda album: f'[{album.id}]{album.title}'
opt = create_option_by_file(optionFile)
client: JmApiClient = opt.new_jm_client() 

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class cfgFileChangeHandler(FileSystemEventHandler):
    def __init__(self, observer):
        self.observer = observer

    def on_modified(self, event):
        if not event.is_directory and Path(optionFile).exists():
            global opt, pdf_dir, client 
            opt = create_option_by_file(optionFile)
            client = opt.new_jm_client()
            print("配置文件已更新")


observer = Observer()
observer.schedule(cfgFileChangeHandler(observer), path=optionFile, recursive=False) 
observer.start()

from flask import Flask, jsonify, request, send_file, redirect # 导入 request, send_file 和 redirect
from waitress import serve

from album_service import get_album_pdf_path

app = Flask(__name__)

@app.route('/get_pdf/<jm_album_id>', methods=['GET'])
def get_pdf(jm_album_id):
    passwd_str = request.args.get('passwd', 'true').lower()
    enable_pwd = passwd_str not in ('false', '0')
    try:
        titletype = int(request.args.get('Titletype', 1))
    except ValueError:
        titletype = 1 
    output_pdf_directly = request.args.get('pdf', 'false').lower() == 'true'

    path, name = get_album_pdf_path(jm_album_id, pdf_dir, opt, enable_pwd=enable_pwd, Titletype=titletype)
    if path is None:
        return jsonify({
            "success": False,
            "message": "PDF 文件不存在"
        }),

    if output_pdf_directly:
        try:
            return send_file(path, as_attachment=True, download_name=name)
        except Exception as e:
            return jsonify({
                "success": False,
                "message": f"发送 PDF 文件时出错: {e}"
            }), 500
    else:
        try:
            with open(path, "rb") as f:
                encoded_pdf = base64.b64encode(f.read()).decode('utf-8')
            return jsonify({
                "success": True,
                "message": "PDF 获取成功",
                "name": name,
                "data": encoded_pdf
            })
        except Exception as e:
            return jsonify({
                "success": False,
                "message": f"读取或编码 PDF 文件时出错: {e}"
            }), 500


import os


@app.route('/get_pdf_path/<jm_album_id>', methods=['GET'])
def get_pdf_path(jm_album_id):
    passwd_str = request.args.get('passwd', 'true').lower()
    enable_pwd = passwd_str not in ('false', '0')
    try:
        titletype = int(request.args.get('Titletype', 1))
    except ValueError:
        titletype = 1 # 

    path, name = get_album_pdf_path(jm_album_id, pdf_dir, opt, enable_pwd=enable_pwd, Titletype=titletype)
    abspath = (os.path.abspath(path))
    if path is None:
        return jsonify({
            "success": False,
            "message": "PDF 文件不存在"
        }), 500
    else:
        return jsonify({
            "success": True,
            "message": "PDF 获取成功",
            "data": abspath,
            "name": name
        })


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
    'cosplay': JmMagicConstants.CATEGORY_DOUJIN_COSPLAY, # 别名
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


@app.route('/search', methods=['GET'])
def search_comics():
    query = request.args.get('query')
    page_num = request.args.get('page', 1, type=int)

    if not query:
        return jsonify({"success": False, "message": "Missing 'query' parameter"}), 400

    try:
        page: JmSearchPage = client.search_site(search_query=query, page=page_num)
        results = [{"id": album_id, "title": title} for album_id, title in page]

        has_next_page = False
        try:
            from itertools import tee
            page_iter_copy, page_iter_check = tee(page) 
            next(page_iter_check)
            next_page_check = client.search_site(search_query=query, page=page_num + 1)
            next(iter(next_page_check)) 
            has_next_page = True
            page = page_iter_copy

        except StopIteration: 
             has_next_page = False
        except JmcomicException as e: 
             print(f"Check for next page failed (JmcomicException): {e}")
             has_next_page = False
        except Exception as e:
             print(f"Check for next page failed (Exception): {e}")
             has_next_page = False

        results = [{"id": album_id, "title": title} for album_id, title in page]

        return jsonify({
            "success": True,
            "message": "Search successful",
            "data": {
                "results": results,
                "current_page": page_num,
                "has_next_page": has_next_page
            }
        })
    except JmcomicException as e: 
        return jsonify({"success": False, "message": f"Jmcomic search error: {e}"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

@app.route('/album/<jm_album_id>', methods=['GET'])
def get_album_details(jm_album_id):
    try:
        album: JmAlbumDetail = client.get_album_detail(jm_album_id)

        if not album:
             print(f"get_album_detail failed for {jm_album_id}, trying search_site as fallback.")
             page: JmSearchPage = client.search_site(search_query=jm_album_id)
             results_list = list(page)
             if not results_list:
                 return jsonify({"success": False, "message": f"Album with ID '{jm_album_id}' not found via search either."}), 404
             
             first_result_id, _ = results_list[0]
             if str(first_result_id) == str(jm_album_id):

                 album = client.get_album_detail(jm_album_id)
                 if not album:
                      return jsonify({"success": False, "message": f"Could not retrieve details for album ID '{jm_album_id}' even after search confirmation."}), 500
             else:
                  return jsonify({"success": False, "message": f"Search found results, but none matched the exact ID '{jm_album_id}'."}), 404


        return jsonify({
            "success": True,
            "message": "Album details retrieved",
            "data": {
                "id": album.id,
                "title": album.title,
                "tags": album.tags
            }
        })
    except JmcomicException as e: 
        from jmcomic.jm_exception import MissingAlbumPhotoException
        if isinstance(e, MissingAlbumPhotoException) or "not found" in str(e).lower() or "不存在" in str(e):
             return jsonify({"success": False, "message": f"Album with ID '{jm_album_id}' not found (JmcomicException: {e})."}), 404
        return jsonify({"success": False, "message": f"Jmcomic error retrieving details: {e}"}), 500
    except Exception as e:
        import traceback
        print(f"Unexpected error in get_album_details for {jm_album_id}:")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An unexpected server error occurred: {e}"}), 500

@app.route('/docs')
def redirect_to_docs():
    """
    Redirects to the external API documentation.
    """
    return redirect("https://jm-api.apifox.cn")

@app.route('/categories', methods=['GET'])
def get_categories():
    page_num = request.args.get('page', 1, type=int)
    time_str = request.args.get('time', 'all').lower()
    category_str = request.args.get('category', 'all').lower()
    order_by_str = request.args.get('order_by', 'latest').lower()

    time_param = TIME_MAP.get(time_str, DEFAULT_TIME)
    category_param = CATEGORY_MAP.get(category_str, DEFAULT_CATEGORY)
    order_by_param = ORDER_BY_MAP.get(order_by_str, DEFAULT_ORDER_BY)

    try:
        page: JmCategoryPage = client.categories_filter(
            page=page_num,
            time=time_param,
            category=category_param,
            order_by=order_by_param,
        )

        has_next_page = False
        try:
            from itertools import tee
            page_iter_copy, page_iter_check = tee(page)
            next(page_iter_check) 
            next_page_check = client.categories_filter(
                page=page_num + 1,
                time=time_param,
                category=category_param,
                order_by=order_by_param,
            )
            next(iter(next_page_check)) 
            has_next_page = True
            page = page_iter_copy 
        except StopIteration:
            has_next_page = False
        except JmcomicException as e:
             print(f"Check for next category page failed (JmcomicException): {e}")
             has_next_page = False
        except Exception as e:
             print(f"Check for next category page failed (Exception): {e}")
             has_next_page = False

        results = [{"id": album_id, "title": title} for album_id, title in page]

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
                }
            }
        })
    except JmcomicException as e:
        return jsonify({"success": False, "message": f"Jmcomic categories error: {e}"}), 500
    except Exception as e:
        import traceback
        print(f"Unexpected error in get_categories:")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An unexpected server error occurred: {e}"}), 500

@app.route('/docs')
def redirect_to_docs():
    """
    Redirects to the external API documentation.
    """
    return redirect("https://jm-api.apifox.cn")


if __name__ == '__main__':
    serve(app, host=host, port=port)
