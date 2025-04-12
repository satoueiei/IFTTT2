from flask import Flask
from threading import Thread
import logging
import os

log = logging.getLogger('werkzeug'); log.setLevel(logging.ERROR)
cli = logging.getLogger('flask.cli'); cli.setLevel(logging.ERROR)

app = Flask('')
@app.route('/')
def home(): return "Bot is running!"
def run(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)))
def keep_alive():
    server_thread = Thread(target=run); server_thread.daemon = True; server_thread.start()
    print(f"Keep alive server started on port {os.environ.get('PORT', 3000)}.")