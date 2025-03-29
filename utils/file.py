import re
from pathlib import Path

# 判断本子是否已存在
# 若存在则返回title(否则返回None)
# @param path: 本子所在目录
# @param jm_album_id: 本子ID
# @return: title: str or None
# @TR0MXI
def IsJmBookExist(path, jm_album_id):
    """
    检查本子是否存在
    :param jm_album_id: 本子ID
    :return: bool
    """
    # 检查path是否存在
    if not Path(path).exists():
        return None

    # 根据本子文件夹命名规则[id]title
    # 使用正则表达式匹配[id]来判断本子是否存在
    path = Path(path)
    pattern = re.compile(rf"\[{jm_album_id}\]")
    for item in path.iterdir():
        if item.is_dir() and pattern.match(item.name):
            # 如果是文件夹且符合规则，则返回title
            return item.name
    return None