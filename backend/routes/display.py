#routes/display.py

from flask import Blueprint, request, jsonify, render_template_string
from datetime import datetime
import json
from db.redis_mongo import redis_client, clinics_col
from queue_service.queue_engine import today_key

display_bp = Blueprint("display_bp", __name__)



@display_bp.route("/display_data", methods=["GET"])
def display_data():
    clinic_id = request.args.get("clinic_id")
    if not clinic_id:
        return jsonify({"status": "error", "message": "clinic_id required"}), 400

    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        return jsonify({"status": "error", "message": "Clinic not found"}), 404

    today = today_key()

    # ---------------- ACTIVE ----------------
    active = []
    pattern = f"active:{clinic_id}:{today}:*"
    for key in redis_client.keys(pattern):
        data = redis_client.hgetall(key)
        active.append({
            "token": data.get("token", ""),
            "name": data.get("name", "IN CONSULTATION")
        })

    # ---------------- QUEUE ----------------
    queue = []
    queue_key = f"queue:{clinic_id}:{today}"
    for item in redis_client.zrange(queue_key, 0, -1):
        p = json.loads(item)
        queue.append({
            "token": p.get("token", ""),
            "name": p["name"]
        })

    # ---------------- BUFFER EARLY ----------------
    buffer_early = []
    early_key = f"buffer:early:{clinic_id}:{today}"
    for item in redis_client.zrange(early_key, 0, -1):
        p = json.loads(item)
        buffer_early.append({
            "token": p.get("token", ""),
            "name": p["name"]
        })

    # ---------------- BUFFER LATE ----------------
    buffer_late = []
    late_key = f"buffer:late:{clinic_id}:{today}"
    for item in redis_client.zrange(late_key, 0, -1):
        p = json.loads(item)
        buffer_late.append({
            "token": p.get("token", ""),
            "name": p["name"]
        })

    return jsonify({
        "time": datetime.now().strftime("%H:%M:%S"),
        "active": active,
        "queue": queue,
        "buffer_early": buffer_early,
        "buffer_late": buffer_late
    })




@display_bp.route("/display_ui", methods=["GET"])
def display_ui():
    clinic_id = request.args.get("clinic_id")
    if not clinic_id:
        return "clinic_id missing", 400

    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Clinic Queue Display</title>
    <style>
        body {
            margin: 0;
            background: #000;
            color: #fff;
            font-family: Arial;
        }
        .container {
            display: flex;
            height: 100vh;
        }
        .zone {
            flex: 1;
            padding: 20px;
            border-right: 2px solid #333;
            overflow-y: auto;
        }
        .zone:last-child {
            border-right: none;
        }
        h2 {
            text-align: center;
            border-bottom: 2px solid #444;
            padding-bottom: 10px;
            margin-bottom: 15px;
        }
        .item {
            font-size: 26px;
            margin: 15px 0;
        }
        .token {
            color: #00ffcc;
            font-weight: bold;
        }
        .time {
            position: fixed;
            bottom: 10px;
            right: 20px;
            color: #aaa;
        }
        .empty {
            color: #777;
            font-size: 22px;
            text-align: center;
            margin-top: 40px;
        }
    </style>
</head>
<body>

<div class="container">
    <div class="zone">
        <h2>ACTIVE</h2>
        <div id="active"></div>
    </div>
    <div class="zone">
        <h2>WAITING</h2>
        <div id="queue"></div>
    </div>
    <div class="zone">
        <h2>UPCOMING</h2>
        <div id="buffer_early"></div>
    </div>
    <div class="zone">
        <h2>OVERFLOW</h2>
        <div id="buffer_late"></div>
    </div>
</div>

<div class="time" id="time"></div>

<script>
const params = new URLSearchParams(window.location.search);
const clinicId = params.get("clinic_id");

function renderList(el, list, emptyText) {
    el.innerHTML = "";
    if (list.length === 0) {
        el.innerHTML = "<div class='empty'>" + emptyText + "</div>";
        return;
    }
    list.forEach(p => {
        el.innerHTML +=
            "<div class='item'>" +
                "<span class='token'>TOKEN " + p.token + "</span><br/>" +
                p.name +
            "</div>";
    });
}

async function refresh() {
    const res = await fetch(`/display_data?clinic_id=${clinicId}`);
    const data = await res.json();

    document.getElementById("time").innerText = data.time;

    renderList(
        document.getElementById("active"),
        data.active,
        "No Active Patient"
    );

    renderList(
        document.getElementById("queue"),
        data.queue,
        "No Waiting Patients"
    );

    renderList(
        document.getElementById("buffer_early"),
        data.buffer_early,
        "No Upcoming Patients"
    );

    renderList(
        document.getElementById("buffer_late"),
        data.buffer_late,
        "No Overflow Patients"
    );
}

setInterval(refresh, 3000);
refresh();
</script>

</body>
</html>
"""
    return html
