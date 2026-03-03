"""
dashboard/app.py  –  Flask web dashboard with Server-Sent Events (SSE).

Routes
------
GET /           → dashboard HTML page
GET /stream     → SSE stream (data pushed every DASHBOARD_SSE_INTERVAL_S)
GET /health     → JSON health check for all sensors
GET /snapshot   → JSON single latest reading (useful for debugging)
"""

import json
import logging
import time
from flask import Flask, Response, jsonify, render_template, stream_with_context

import config

logger = logging.getLogger(__name__)

# sensor_manager is injected by main.py after startup
_sensor_manager = None


def create_app(sensor_manager) -> Flask:
    """
    Factory function — creates and configures the Flask app.

    Parameters
    ----------
    sensor_manager : SensorManager
        Already-started SensorManager instance.
    """
    global _sensor_manager
    _sensor_manager = sensor_manager

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = "neurosense-dev-key"

    # ── Routes ────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/stream")
    def stream():
        """
        SSE endpoint.  The browser opens one persistent connection here and
        receives JSON data frames every DASHBOARD_SSE_INTERVAL_S seconds.
        """
        def event_generator():
            while True:
                data = _sensor_manager.get_latest()
                # Convert None to JSON null cleanly
                payload = json.dumps(data, default=lambda x: None)
                yield f"data: {payload}\n\n"
                time.sleep(config.DASHBOARD_SSE_INTERVAL_S)

        return Response(
            stream_with_context(event_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control":  "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx buffering if proxied
            },
        )

    @app.route("/health")
    def health():
        """JSON health check — shows per-sensor status."""
        return jsonify({
            "status": "ok",
            "sensors": _sensor_manager.health(),
        })

    @app.route("/snapshot")
    def snapshot():
        """Return a single JSON snapshot of latest sensor readings."""
        return jsonify(_sensor_manager.get_latest())

    return app
