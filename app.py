# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, json, base64, time
from threading import Thread

app = Flask(__name__)
CORS(app)  # Permitir cualquier origen (frontend)

# ----------------------
# Configuración API Telegestión
# ----------------------
API_GATEWAY_HOST = "https://www.api.telegestion-idc.uy:9081"
APP_KEY = "eric.montelongo@imcanelones.gub.uy"
APP_SECRET = "dgVmdLvqiCMOBpqTyUcFFjwJS0fWOxwz"
SCOPE = "read"
BASIC_AUTH = base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()
RESOURCE_ID = "b6213e15-3c3f-4bb0-9017-8b793cbae407"

# ----------------------
# Token en memoria
# ----------------------
_cached_token = None
_token_expiry = 0

def get_cached_access_token():
    global _cached_token, _token_expiry
    if _cached_token and time.time() < _token_expiry:
        return _cached_token

    url_token = f"{API_GATEWAY_HOST}/oauth/accesstoken"
    data = f"service=city&app_key={APP_KEY}&app_secret={APP_SECRET}&scope={SCOPE}"
    headers_token = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {BASIC_AUTH}"
    }
    res = requests.post(url_token, headers=headers_token, data=data)
    res.raise_for_status()
    token_data = res.json()
    token = token_data.get("token")
    expires_in = token_data.get("expires_in", 3600)
    if not token:
        raise Exception("No se obtuvo token")
    _cached_token = token
    _token_expiry = time.time() + int(expires_in) - 10
    return token

# ----------------------
# RTP Async Storage
# ----------------------
rtp_status = {}  # { handleId: { "antenna": "...", "status": "PENDING"/"DONE"/"ERROR", "data": [...] } }

# ----------------------
# Endpoint RTP: iniciar
# ----------------------
@app.route("/api/rtp/start", methods=["GET"])
def start_rtp():
    antenna = request.args.get("antenna")
    if not antenna:
        return jsonify({"error": "Falta parámetro 'antenna'"}), 400

    try:
        token = get_cached_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{API_GATEWAY_HOST}/interact/api/city/realtimelink/v1.0/en-us/{RESOURCE_ID}"

        posibles_ids = [antenna, antenna.replace(":", "")]
        handle_id = None
        for cid in posibles_ids:
            payload = [{"componentExternalId": cid, "componentType": "OLC"}]
            res = requests.post(url, headers=headers, json=payload)
            if res.ok:
                data = res.json()
                if isinstance(data, list) and len(data) > 0 and "handleId" in data[0]:
                    handle_id = data[0]["handleId"]
                    break

        if not handle_id:
            return jsonify({"error": "No se pudo obtener handleId"}), 500

        rtp_status[int(handle_id)] = {"antenna": antenna, "status": "PENDING", "data": None}

        
        # Polling en background
        def poll_details(handle_id):
            url_details = f"{API_GATEWAY_HOST}/interact/api/city/realtimelink/v1.0/en-us/{RESOURCE_ID}/details?handleId={handle_id}"
            MAX_ATTEMPTS = 6       # máximo de intentos
            POLL_INTERVAL = 5     # segundos entre intentos
            last_data = None

            for attempt in range(MAX_ATTEMPTS):
                try:
                    details_res = requests.get(url_details, headers={"Authorization": f"Bearer {token}"})
                    if details_res.status_code == 200:
                        details_json = details_res.json()
                        if isinstance(details_json, list) and len(details_json) > 0:
                            props = details_json[0].get("properties", [])
                            num_props = len(props)
                            print(f"[DEBUG] Intento {attempt+1}: {num_props} propiedades recibidas")
                            last_data = details_json

                            # ---- Criterio de corte ----
                            if num_props > 20:
                                print("[DEBUG] Más de 20 propiedades recibidas → stop polling")
                                break
                            if num_props == 1:
                                print("[DEBUG] Solo 1 propiedad (inaccesible) → stop polling")
                                break
                except Exception as e:
                    print(f"[DEBUG] Error en intento {attempt+1}: {e}")

                time.sleep(POLL_INTERVAL)

            if last_data:
                rtp_status[int(handle_id)]["status"] = "DONE"
                rtp_status[int(handle_id)]["data"] = last_data
            else:
                rtp_status[int(handle_id)]["status"] = "ERROR"

        Thread(target=poll_details, args=(handle_id,), daemon=True).start()

        return jsonify({"handleId": handle_id, "status": "PENDING"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------------
# Endpoint RTP: estado
# ----------------------
@app.route("/api/rtp/status", methods=["GET"])
def rtp_status_check():
    handle_id = request.args.get("handleId")
    if not handle_id or int(handle_id) not in rtp_status:
        return jsonify({"error": "handleId no encontrado"}), 404
    return jsonify(rtp_status[int(handle_id)])

# ----------------------
# Encender luminaria
# ----------------------
@app.route("/api/control/on", methods=["POST"])
def control_on():
    data = request.get_json()
    control_id = data.get("controlId")
    light_level = data.get("lightLevel")
    reset_hours = data.get("resetHours")

    if not control_id or light_level is None or reset_hours is None:
        return jsonify({"success": False, "message": "Faltan parámetros"}), 400

    try:
        token = get_cached_access_token()
        payload = {"ExternalId": control_id, "Level": light_level, "ResetInHours": reset_hours}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

        TELEG_API = f"{API_GATEWAY_HOST}/interact/api/city/controllink/v1.0/en-us/{RESOURCE_ID}/setlightlevelforstreetlight"
        res = requests.post(TELEG_API, headers=headers, json=payload)
        if res.ok:
            return jsonify(res.json())
        return jsonify({"success": False, "message": f"Error externo: {res.status_code}"}), res.status_code
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ----------------------
# Apagar luminaria
# ----------------------
@app.route("/api/control/off", methods=["POST"])
def control_off():
    data = request.get_json()
    control_id = data.get("controlId")
    if not control_id:
        return jsonify({"success": False, "message": "Falta parámetro controlId"}), 400

    try:
        token = get_cached_access_token()
        payload = {"ExternalId": control_id}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

        TELEG_API = f"{API_GATEWAY_HOST}/interact/api/city/controllink/v1.0/en-us/{RESOURCE_ID}/resetstreetlight"
        res = requests.post(TELEG_API, headers=headers, json=payload)
        if res.ok:
            return jsonify(res.json())
        return jsonify({"success": False, "message": f"Error externo: {res.status_code}"}), res.status_code
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ----------------------
# Ping rápido
# ----------------------
@app.route("/api/ping")
def ping():
    return jsonify({"ok": True})

# ----------------------
# Run
# ----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
