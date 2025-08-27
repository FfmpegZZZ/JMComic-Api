from flask import Flask
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path
from jmcomic import create_option_by_file
from app.core.config import config
from app.api.routes import bp as api_bp


class OptionReloader(FileSystemEventHandler):
    def __init__(self, option_path: Path, reload_callback):
        self.option_path = option_path
        self.reload_callback = reload_callback

    def on_modified(self, event):
        if not event.is_directory and Path(event.src_path) == self.option_path:
            self.reload_callback()


def create_app():
    app = Flask(__name__)
    app.register_blueprint(api_bp)

    # store client/opt in app context for future extension if needed
    state = {}

    def load_option():
        opt = create_option_by_file(config.option_file)
        state['opt'] = opt
        state['client'] = opt.new_jm_client()
        print("配置文件已更新")

    load_option()

    observer = Observer()
    observer.schedule(OptionReloader(Path(config.option_file), load_option), path=str(Path(config.option_file).parent or '.'), recursive=False)
    observer.start()

    app.config['observer'] = observer
    app.config['state'] = state

    return app
