from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, json, base64, time

app = Flask(__name__)
CORS(app)

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
_token_expiry = 0  # timestamp de expiración

def get_cached_access_token():
    global _cached_token, _token_expiry

    # Si el token existe y no expiró, lo usamos
    if _cached_token and time.time() < _token_expiry:
        return _cached_token

    # Si no, pedimos uno nuevo
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
    expires_in = token_data.get("expires_in", 3600)  # tiempo en segundos (por defecto 1h)

    if not token:
        raise Exception("No se obtuvo token")

    # Guardamos token y expiración
    _cached_token = token
    _token_expiry = time.time() + int(expires_in) - 10  # restamos 10s de margen

    return token

# ----------------------
# Transformación simple de RTP
# ----------------------
def transform_rtp_data(rtp_details):
    # Copiar campos principales
    transformed = {
        "assetId": rtp_details.get("assetId"),
        "status": rtp_details.get("status"),
        "handleId": rtp_details.get("handleId"),
        "DateTime": rtp_details.get("DateTime"),
    }

    # Desglosar la lista de properties en claves individuales
    for prop in rtp_details.get("properties", []):
        key = prop.get("Key")
        value = prop.get("Value")
        if key:
            transformed[key] = value

    return transformed

# ----------------------
# Endpoint para iniciar RTP y obtener handleId
# ----------------------
@app.route("/api/rtp/start", methods=["GET"])
def start_rtp():
    antenna = request.args.get("antenna")
    if not antenna:
        return jsonify({"error": "Falta parámetro 'antenna'"}), 400

    try:
        access_token = get_cached_access_token()  # token cacheado
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

        url = f"{API_GATEWAY_HOST}/interact/api/city/realtimelink/v1.0/en-us/{RESOURCE_ID}"

        posibles_ids = [
            antenna,               # con :
            antenna.replace(":", "")  # sin :
        ]

        handle_id = None
        for cid in posibles_ids:
            payload = [{"componentExternalId": cid, "componentType": "OLC"}]
            print(f"[DEBUG] Probando con componentExternalId={cid}")

            try:
                res = requests.post(url, headers=headers, json=payload)
                print(f"[DEBUG] Status {res.status_code} - {res.text}")
                if res.ok:
                    data = res.json()
                    if isinstance(data, list) and len(data) > 0 and "handleId" in data[0]:
                        handle_id = data[0]["handleId"]
                        break
            except Exception as e:
                print(f"[ERROR] Intento fallido con {cid}: {e}")

        if not handle_id:
            return jsonify({"error": "No se pudo obtener handleId con ninguno de los formatos"}), 500

        # Polling para obtener detalles
        MAX_ATTEMPTS = 2
        POLL_INTERVAL = 25  # segundos
        details_data = None

        url_details = f"{API_GATEWAY_HOST}/interact/api/city/realtimelink/v1.0/en-us/{RESOURCE_ID}/details?handleId={handle_id}"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                details_res = requests.get(url_details, headers={"Authorization": f"Bearer {access_token}"})
                print(f"[DEBUG] Intento {attempt}/{MAX_ATTEMPTS}, status={details_res.status_code}")

                if details_res.status_code == 200:
                    details_json = details_res.json()
                    time.sleep(3)
                    if isinstance(details_json, list) and len(details_json) > 0:
                        print(f"[DEBUG] Se recibieron {len(details_json)} items")
                        details_data = details_json  # devolver todo lo que llega
                        break
            except Exception as e:
                print(f"[ERROR] Intento {attempt} fallido: {e}")

            time.sleep(POLL_INTERVAL)

        if not details_data:
            return jsonify({"error": "No se pudieron obtener los datos después de varios intentos."}), 500

        return jsonify(details_data)
    except Exception as e:
        print("[ERROR] Fallo al obtener RTP completo:", e)
        return jsonify({"error": str(e)}), 500

# ----------------------
# Endpoint para encender luminaria
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
        if not token:
            return jsonify({"success": False, "message": "No se pudo obtener token"}), 500

        payload = {
            "ExternalId": control_id,
            "Level": light_level,
            "ResetInHours": reset_hours
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"  # Aquí usamos el token
        }

        TELEGESTION_API_URL = "https://www.api.telegestion-idc.uy:9081/interact/api/city/controllink/v1.0/en-us/b6213e15-3c3f-4bb0-9017-8b793cbae407/setlightlevelforstreetlight"
        res = requests.post(TELEGESTION_API_URL, headers=headers, json=payload)

        if res.ok:
            return jsonify(res.json())
        else:
            return jsonify({
                "success": False,
                "message": f"Error del sistema externo: {res.status_code} - {res.text}"
            }), res.status_code

    except Exception as e:
        print(f"[ERROR] Fallo al encender luminaria: {e}")
        return jsonify({"success": False, "message": "Falla en la comunicación con el servidor proxy."}), 500

# ----------------------
# Endpoint para apagar luminaria
# ----------------------
@app.route("/api/control/off", methods=["POST"])
def control_off():
    data = request.get_json()
    control_id = data.get("controlId")

    if not control_id:
        return jsonify({"success": False, "message": "Falta parámetro controlId"}), 400

    try:
        token = get_cached_access_token()
        if not token:
            return jsonify({"success": False, "message": "No se pudo obtener token"}), 500

        payload = {
            "ExternalId": control_id
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        TELEGESTION_API_URL = "https://www.api.telegestion-idc.uy:9081/interact/api/city/controllink/v1.0/en-us/b6213e15-3c3f-4bb0-9017-8b793cbae407/resetstreetlight"

        res = requests.post(TELEGESTION_API_URL, headers=headers, json=payload)

        if res.ok:
            return jsonify(res.json())
        else:
            return jsonify({
                "success": False,
                "message": f"Error del sistema externo: {res.status_code} - {res.text}"
            }), res.status_code

    except Exception as e:
        print(f"[ERROR] Fallo al resetear luminaria: {e}")
        return jsonify({"success": False, "message": "Falla en la comunicación con el servidor proxy."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
