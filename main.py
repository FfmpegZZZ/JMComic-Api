from waitress import serve
from app.core.config import config
from app.factory import create_app

app = create_app()

if __name__ == '__main__':
    serve(app, host=config.host, port=config.port)
