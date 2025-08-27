import re
from pathlib import Path

def is_jm_book_exist(path: str, jm_album_id: str):
    """Return title if album folder exists, else None.

    Folder naming convention: [id]title
    jm_album_id may start with 'JM'; we ignore that.
    """
    root = Path(path)
    if not root.exists():
        return None
    clean_id = jm_album_id.removeprefix("JM")
    pattern = re.compile(rf"\[{clean_id}\]")
    for item in root.iterdir():
        if item.is_dir() and pattern.match(item.name):
            first_bracket_index = item.name.find(']')
            if first_bracket_index != -1:
                return item.name[first_bracket_index + 1 :].strip()
            return None
    return None
