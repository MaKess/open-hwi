import logging
from queue import Queue

from gevent.pywsgi import WSGIServer
from flask import Flask, config
from flask_socketio import SocketIO

# apt install python3-flask python3-flask-socketio python3-gevent

def create_app(name: str, receive_queue: Queue):
    socketio = SocketIO()
    app = Flask(name)
    app.config["receive_queue"] = receive_queue

    @app.route("/")
    def hello_world():
        return "<p>Hello, World!</p>"

    socketio.init_app(app)

    @socketio.on("trololo")
    def trololo_handler():
        pass # someone did a trololo

    return app

def serve(name: str,
          logger: logging.Logger,
          receive_queue: Queue):
    WSGIServer(
        listener=("0.0.0.0", 8000), # TODO: get this from the ConfigProvider
        application=create_app(name=name,
                               receive_queue=receive_queue),
        log=logger,
        error_log=logger
    ).serve_forever()
