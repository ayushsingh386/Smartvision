#app_server.py
import cv2
import base64
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO
from ultralytics import YOLO
import numpy as np
import time
import geocoder
import navigator
import os 

# Load the API key from an environment variable
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', 'AIzaSyBfuKJnf_VxFQuiuGt91McKHOOPje84RQU')
if GOOGLE_MAPS_API_KEY == 'AIzaSyBfuKJnf_VxFQuiuGt91McKHOOPje84RQU':
    print("Warning: GOOGLE_MAPS_API_KEY environment variable not set. Using a placeholder key.")

navigator.set_google_api_key(GOOGLE_MAPS_API_KEY)

KNOWN_WIDTH = 0.4
FOCAL_LENGTH = 800
ALERT_DISTANCE = 2.0
ALERT_COOLDOWN = 5

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_for_navigator_app'
socketio = SocketIO(app)

try:
    model = YOLO('yolov8n.pt')
except Exception as e:
    print(f"Error loading YOLO model: {e}")
    model = None

clients = {}
video_capture = None
last_alert_time = {}



#object detection
def obstacle_detection_thread(sid):
    """Processes video feed for obstacle detection for a specific client."""
    global video_capture
    if video_capture is None:
        video_capture = cv2.VideoCapture(0)
    
    client = clients.get(sid)
    if not client: return
    
    print(f"Obstacle detection thread started for {sid}.")
    while client.get('is_detection_running'):
        if not video_capture.isOpened():
            print("Error: Cannot open webcam.")
            time.sleep(1)
            continue
        
        success, frame = video_capture.read()
        if not success:
            time.sleep(0.1)
            continue
            
        results = model(frame, verbose=False) if model else []
        obstacles, is_obstacle_near = [], False
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                class_name = model.names[int(box.cls[0])]
                distance = (KNOWN_WIDTH * FOCAL_LENGTH) / (x2 - x1 if x2 > x1 else 1)
                is_near = distance < ALERT_DISTANCE
                if is_near:
                    is_obstacle_near = True
                    current_time = time.time()
                    if class_name not in last_alert_time or (current_time - last_alert_time.get(class_name, 0)) > ALERT_COOLDOWN:
                        alert_message = f"Warning: {class_name} detected {distance:.1f} meters ahead."
                        socketio.emit('speak_alert', {'message': alert_message}, to=sid)
                        last_alert_time[class_name] = current_time
                obstacles.append({"box": [x1, y1, x2, y2], "label": f"{class_name}: {distance:.1f}m", "is_near": is_near})
        
        _, buffer = cv2.imencode('.jpg', frame)
        frame_b64 = base64.b64encode(buffer).decode('utf-8')
        socketio.emit('update', {'image': frame_b64, 'obstacles': obstacles, 'status': "OBSTACLE NEAR!" if is_obstacle_near else "SCANNING"}, to=sid)
        socketio.sleep(0.05)
    print(f"Obstacle detection thread stopped for {sid}.")

#routes
@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f'Client connected: {sid}')
    clients[sid] = {
        'is_detection_running': True,
        'is_gps_running': True,
        'current_location': None,
        'location_lock': threading.Lock()
    }
    socketio.start_background_task(target=obstacle_detection_thread, sid=sid)
    # FIX: Removed the inaccurate GPS simulation thread. Real GPS data will now be pushed from the client.

@socketio.on('set_initial_location')
def handle_set_initial_location(data):
    sid = request.sid
    client = clients.get(sid)
    if not client: return
    
    lat, lng = data['lat'], data['lng']
    print(f"Received initial location from browser for {sid}: ({lat}, {lng})")
    with client['location_lock']:
        client['current_location'] = (lat, lng)
    
    try:
        g = geocoder.google([lat, lng], method='reverse', key=GOOGLE_MAPS_API_KEY, language='en')
        address = g.address if g.ok else "Address not found"
    except Exception as e:
        address = "Could not reverse geocode address"
        print(f"Reverse geocoding error: {e}")

    socketio.emit('initial_location', {'lat': lat, 'lng': lng, 'address': address}, to=sid)

# NEW: Handler for real-time, accurate GPS updates from the browser.
@socketio.on('realtime_gps_update')
def handle_realtime_gps_update(data):
    sid = request.sid
    client = clients.get(sid)
    if not client: return
    
    lat, lng = data['lat'], data['lng']
    with client['location_lock']:
        client['current_location'] = (lat, lng)

@socketio.on('location_error_fallback')
def handle_location_error_fallback():
    sid = request.sid
    client = clients.get(sid)
    if not client: return
    
    print(f"Browser location failed for {sid}. Falling back to IP-based location.")
    latlng, address = navigator.get_location_by_ip()
    if latlng:
        with client['location_lock']:
            client['current_location'] = latlng
        socketio.emit('initial_location', {'lat': latlng[0], 'lng': latlng[1], 'address': address}, to=sid)
    else:
        socketio.emit('location_error', {'message': address}, to=sid)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f'Client disconnected: {sid}')
    if sid in clients:
        clients[sid]['is_detection_running'] = False
        clients[sid]['is_gps_running'] = False
        clients.pop(sid)
    
    global video_capture
    if not clients and video_capture is not None:
        video_capture.release()
        video_capture = None
        print("All clients disconnected. Releasing webcam.")

@socketio.on('start_navigator')
def handle_start_navigator(data):
    sid = request.sid
    destination = data.get('destination')
    if destination:
        print(f"Starting navigator to: {destination} for client {sid}")
        socketio.start_background_task(target=navigator.navigation_thread, destination=destination, sid=sid, clients=clients, socketio=socketio)

if __name__ == '__main__':
    print("Starting Flask server...")
    print("Open your web browser and go to http://127.0.0.1:5000")
    socketio.run(app, host='0.0.0.0', port=5000, use_reloader=False, debug=False)

