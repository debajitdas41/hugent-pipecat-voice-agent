#routes/walkin.py
from flask import Blueprint, request, jsonify, render_template
from services.walkin_service import (
    estimate_walkin,
    confirm_walkin
)

walkin_bp = Blueprint("walkin_bp", __name__, url_prefix="/walkin")


@walkin_bp.route("/", methods=["GET"])
def walkin_page():
    clinic_id = request.args.get("clinic_id")
    if not clinic_id:
        return "clinic_id missing", 400
    return render_template("walkin.html", clinic_id=clinic_id)


@walkin_bp.route("/estimate", methods=["POST"])
def walkin_estimate():
    data = request.json
    result = estimate_walkin(data)
    return jsonify(result), 200


@walkin_bp.route("/confirm", methods=["POST"])
def walkin_confirm():
    data = request.json
    result = confirm_walkin(data)
    return jsonify(result), 200
