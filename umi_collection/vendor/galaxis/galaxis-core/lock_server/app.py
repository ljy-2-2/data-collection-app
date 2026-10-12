import json

from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


def load_config():
    with open("config.json") as f:
        return json.load(f)


@app.route("/v1/robot", methods=["GET"])
def get_robots():
    config = load_config()
    return jsonify({"robots": config["robots"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=28080)
