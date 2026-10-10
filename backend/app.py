

import base64
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

try:
    import boto3
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

app = Flask(__name__)
CORS(app)

app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
COINS_TABLE = os.environ.get("COINS_TABLE", "waste-snap-coins")
REDEMPTIONS_TABLE = os.environ.get("REDEMPTIONS_TABLE", "waste-snap-redemptions")
USERS_TABLE = os.environ.get("USERS_TABLE", "waste-snap-users")

# Rewards catalog (demo). Users spend coins via POST /api/redeem.
REWARDS = [
    {"id": "badge-eco", "title": "Eco Warrior Badge", "cost": 50,
     "description": "A digital badge for your Waste Snap profile."},
    {"id": "tree-cert", "title": "Plant-a-Tree Certificate", "cost": 100,
     "description": "We plant a tree in your name."},
    {"id": "voucher-20", "title": "Rs. 20 Voucher", "cost": 200,
     "description": "Demo voucher (hackathon)."},
]

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

PROMPT = """You are a waste-sorting assistant. Look at the image and answer ONLY with JSON (no markdown, no extra text):
{
  "waste_type": "dry" | "wet" | "unknown",
  "in_dustbin": true | false,
  "description": "one short sentence describing what you see"
}
Definitions:
- "dry": dry waste such as plastic, paper, cardboard, metal, glass.
- "wet": wet/organic waste such as food scraps, vegetable peels, leftovers.
- "unknown": no waste is visible in the image.
- "in_dustbin": true if the image shows waste being thrown or put into a dustbin, or waste already inside a dustbin. Otherwise false.
"""


def coins_table():
    """Lazy DynamoDB handle so the app still boots without boto3/creds."""
    if not HAS_BOTO3:
        raise RuntimeError("boto3 is not installed. Run: pip install boto3")
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return dynamodb.Table(COINS_TABLE)


def users_table():
    if not HAS_BOTO3:
        raise RuntimeError("boto3 is not installed. Run: pip install boto3")
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return dynamodb.Table(USERS_TABLE)


def current_user():
    """user_id from the signed session cookie, or None if not logged in."""
    return session.get("user_id")


def clean_username(raw: str) -> str:
    name = (raw or "").strip().lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9-]", "", name)
    return name


def analyze_with_gemini(image_bytes: bytes) -> dict:
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            PROMPT,
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
    )
    text = (response.text or "").strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


@app.route("/", methods=["GET"])
def index():
    page = FRONTEND_DIR / "index.html"
    if not page.exists():
        return ("Frontend not found. Expected at ../frontend/index.html "
                "(next to the backend folder).", 500)
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not HAS_GENAI:
        return jsonify({"error": "google-genai is not installed. Run: pip install -r requirements.txt"}), 500
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY is not set. Add it to backend/.env"}), 500

    data = request.get_json(force=True, silent=True) or {}
    image_data = data.get("image", "")
    if "," in image_data:  # strip "data:image/jpeg;base64," prefix
        image_data = image_data.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(image_data)
    except Exception:
        return jsonify({"error": "Invalid image data."}), 400
    if not image_bytes:
        return jsonify({"error": "Empty image."}), 400

    try:
        result = analyze_with_gemini(image_bytes)
    except Exception as e:
        return jsonify({"error": f"Gemini API error: {e}"}), 502

    waste_type = result.get("waste_type", "unknown")
    in_dustbin = bool(result.get("in_dustbin", False))

    if waste_type == "unknown":
        coins = 0
        message = "No waste detected. Please take a clearer photo of the waste."
    elif in_dustbin:
        coins = 10
        message = f"Great job! You put {waste_type} waste in the dustbin. You earned 10 coins!"
    else:
        coins = 0
        message = f"This looks like {waste_type} waste. Please put it in the dustbin!"

    return jsonify({
        "waste_type": waste_type,
        "in_dustbin": in_dustbin,
        "description": result.get("description", ""),
        "coins_earned": coins,
        "message": message,
    })


@app.route("/api/signup", methods=["POST"])
def signup():
    """Create an account. Request JSON: {"username": "...", "password": "..."}.
    Usernames are unique (DynamoDB enforces it atomically); passwords are
    stored only as scrypt hashes, never plain text."""
    try:
        table = users_table()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    body = request.get_json(force=True, silent=True) or {}
    username = clean_username(body.get("username", ""))
    password = body.get("password", "") or ""
    if len(username) < 3 or len(username) > 20:
        return jsonify({"error": "Username must be 3-20 characters (letters, numbers, dash)."}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters."}), 400
    try:
        table.put_item(
            Item={
                "user_id": username,
                "password_hash": generate_password_hash(password),
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(user_id)",  # unique username
        )
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            return jsonify({"error": "Username already taken. Try another."}), 409
        return jsonify({"error": f"DynamoDB error: {e}"}), 502
    session["user_id"] = username  # log in immediately after signup
    return jsonify({"ok": True, "user_id": username})


@app.route("/api/login", methods=["POST"])
def login():
    """Request JSON: {"username": "...", "password": "..."}."""
    try:
        table = users_table()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    body = request.get_json(force=True, silent=True) or {}
    username = clean_username(body.get("username", ""))
    password = body.get("password", "") or ""
    try:
        res = table.get_item(Key={"user_id": username})
    except Exception as e:
        return jsonify({"error": f"DynamoDB error: {e}"}), 502
    item = res.get("Item")
    if not item or not check_password_hash(item.get("password_hash", ""), password):
        return jsonify({"error": "Wrong username or password."}), 401
    session["user_id"] = username
    return jsonify({"ok": True, "user_id": username})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def me():
    """Who is logged in right now? 200 {"user_id": ...} or 401."""
    user_id = current_user()
    if not user_id:
        return jsonify({"error": "Not logged in."}), 401
    return jsonify({"user_id": user_id})


@app.route("/api/coins", methods=["POST"])
def award_coins():
    """Award coins to the LOGGED-IN user after they pick the correct dustbin.
    Request JSON: {"coins": 10}. The user comes from the session, so nobody
    can award coins to someone else's account."""
    user_id = current_user()
    if not user_id:
        return jsonify({"error": "Login required."}), 401
    try:
        table = coins_table()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    body = request.get_json(force=True, silent=True) or {}
    earned = int(body.get("coins", 10))
    try:
        table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="ADD coins :c",  # atomic increment
            ExpressionAttributeValues={":c": earned},
        )
    except Exception as e:
        return jsonify({"error": f"DynamoDB error: {e}"}), 502
    return jsonify({"ok": True, "user_id": user_id, "added": earned})


@app.route("/api/coins/<user_id>", methods=["GET"])
def get_coins(user_id):
    try:
        table = coins_table()
        res = table.get_item(Key={"user_id": user_id})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"DynamoDB error: {e}"}), 502
    balance = int(res.get("Item", {}).get("coins", 0))
    return jsonify({"user_id": user_id, "coins": balance})


@app.route("/api/leaderboard", methods=["GET"])
def leaderboard():
    try:
        table = coins_table()
        items = table.scan().get("Items", [])
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"DynamoDB error: {e}"}), 502
    items.sort(key=lambda x: int(x.get("coins", 0)), reverse=True)
    return jsonify(items[:10])


@app.route("/api/rewards", methods=["GET"])
def list_rewards():
    """Reward catalog the frontend shows on the Rewards screen."""
    return jsonify(REWARDS)


@app.route("/api/redeem", methods=["POST"])
def redeem():
    """Spend the LOGGED-IN user's coins on a reward.
    Request JSON: {"reward_id": "..."}. Atomic: coins are deducted only
    if the balance covers the cost."""
    if not HAS_BOTO3:
        return jsonify({"error": "boto3 is not installed. Run: pip install boto3"}), 500
    user_id = current_user()
    if not user_id:
        return jsonify({"error": "Login required."}), 401
    body = request.get_json(force=True, silent=True) or {}
    reward_id = body.get("reward_id", "").strip()
    reward = next((r for r in REWARDS if r["id"] == reward_id), None)
    if not reward:
        return jsonify({"error": "A valid reward_id is required."}), 400
    try:
        coins_tbl = coins_table()
        coins_tbl.update_item(
            Key={"user_id": user_id},
            UpdateExpression="ADD coins :neg",
            ConditionExpression="coins >= :cost",
            ExpressionAttributeValues={":neg": -reward["cost"], ":cost": reward["cost"]},
        )
        redemptions = boto3.resource("dynamodb", region_name=AWS_REGION).Table(REDEMPTIONS_TABLE)
        now = int(time.time())
        redemptions.put_item(Item={
            "redemption_id": f"{user_id}#{now}",
            "user_id": user_id,
            "reward_id": reward["id"],
            "reward_title": reward["title"],
            "cost": reward["cost"],
            "redeemed_at": now,
        })
        res = coins_tbl.get_item(Key={"user_id": user_id})
        balance = int(res.get("Item", {}).get("coins", 0))
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            return jsonify({"error": "Not enough coins for this reward."}), 400
        return jsonify({"error": f"DynamoDB error: {e}"}), 502
    return jsonify({"ok": True, "reward": reward["title"], "coins": balance})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "gemini_configured": bool(GEMINI_API_KEY),
        "boto3_installed": HAS_BOTO3,
        "aws_region": AWS_REGION,
        "coins_table": COINS_TABLE,
    })


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
