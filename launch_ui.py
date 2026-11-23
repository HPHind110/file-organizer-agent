import secrets
import threading
import webbrowser

import uvicorn

from api_server import create_app
from organizer.dashboard import choose_folder


if __name__ == "__main__":
    folder = choose_folder()
    if folder:
        token = secrets.token_urlsafe(32)
        server = uvicorn.Server(uvicorn.Config(create_app(folder, token, dashboard_picker=True),
                                host="127.0.0.1", port=8101, proxy_headers=False))
        def open_when_ready():
            import time
            while not server.started:
                time.sleep(0.1)
            webbrowser.open("http://127.0.0.1:8101/#" + token)
        threading.Thread(target=open_when_ready, daemon=True).start()
        server.run()
