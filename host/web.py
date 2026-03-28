import logging
from gevent.pywsgi import WSGIServer
from flask import Flask

# apt install python3-flask python3-gevent

def create_app(name):
    app = Flask(name)

    @app.route("/")
    def hello_world():
        return "<p>Hello, World!</p>"

    return app

def serve(name: str, logger: logging.Logger):
    WSGIServer(
        listener=("0.0.0.0", 8000),
        application=create_app(name=name),
        log=logger,
        error_log=logger
    ).serve_forever()