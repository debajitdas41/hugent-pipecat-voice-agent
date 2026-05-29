import os

from dotenv import load_dotenv
from flask import Flask, request
from flask_cors import CORS

# Load env early so flow modules read correct settings
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from flows.webhook_flow import handle_webhook, verify_webhook  # noqa: E402

app = Flask(__name__)

# 🔥 Allow ALL origins (frontend domain + any scanner client)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.route("/webhook", methods=["POST"])
def webhook():
    return handle_webhook(request)


@app.route("/webhook", methods=["GET"])
def verify():
    return verify_webhook(request)


from routes.checkin import checkin_bp  # noqa: E402
from routes.queue import queue_bp  # noqa: E402
from routes.checkout import checkout_bp  # noqa: E402
from routes.display import display_bp  # noqa: E402
from routes.walkin import walkin_bp  # noqa: E402
from routes.notifications import notifications_bp  # noqa: E402
from routes.webchat import webchat_bp  # noqa: E402

app.register_blueprint(checkin_bp)
app.register_blueprint(queue_bp)
app.register_blueprint(checkout_bp)
app.register_blueprint(display_bp)
app.register_blueprint(walkin_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(webchat_bp)


@app.route("/chat")
def webchat_page():
    return app.send_static_file("webchat.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("FLASK_PORT", 5000)), debug=True, use_reloader=False)
