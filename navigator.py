#navigator.py
import threading
import time
import geocoder
import requests
import re
import math
import numpy as np

GOOGLE_MAPS_API_KEY = None
WAYPOINT_PROXIMITY_THRESHOLD = 20

def set_google_api_key(key):
    global GOOGLE_MAPS_API_KEY
    GOOGLE_MAPS_API_KEY = key

#calc lang and long distance
def haversine_distance(coord1, coord2):
    R = 6371000 # Earth radius in meters
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

#location via ip
def get_location_by_ip():
    try:
        g = geocoder.ip('me')
        if g.ok:
            return g.latlng, g.address
        return None, "Could not fetch location via IP."
    except Exception as e:
        return None, f"An error occurred during IP geolocation: {e}"

#track user
def gps_simulation_thread(sid, clients, socketio):
    client = clients.get(sid)
    if not client: return
    
    print(f"GPS Simulation Started for {sid}.")
    while client.get('is_gps_running'):
        with client['location_lock']:
            if not client.get('current_location'):
                time.sleep(1) 
                continue
            
            # observe slight movement
            new_lat = client['current_location'][0] + np.random.uniform(-0.00005, 0.00005)
            new_lng = client['current_location'][1] + np.random.uniform(-0.00005, 0.00005)
            client['current_location'] = (new_lat, new_lng)
            current_pos = client['current_location']

        socketio.emit('gps_update', {'lat': current_pos[0], 'lng': current_pos[1]}, to=sid)
        time.sleep(3)
    print(f"GPS Simulation Stopped for {sid}.")

#get directions from google maps
def get_directions(origin, destination):
    if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == 'YOUR_DEFAULT_API_KEY':
        return None, "Google Maps API key is not configured on the server."
    base_url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {'origin': f"{origin[0]},{origin[1]}", 'destination': destination, 'key': GOOGLE_MAPS_API_KEY, 'mode': 'walking'}
    try:
        response = requests.get(base_url, params=params)
        response.raise_for_status()
        return response.json(), None
    except requests.exceptions.RequestException as e:
        return None, f"API request error: {e}"

# MODIFIED: Removed speak_func from arguments
def navigation_thread(destination, sid, clients, socketio):
    client = clients.get(sid)
    if not client: return

    def emit_and_speak(event, message, speak_text=None):
        socketio.emit(event, {'message': message}, to=sid)
        text_to_say = speak_text if speak_text is not None else message
        clean_text = re.sub('<[^<]+?>', '', text_to_say)
        # MODIFIED: Emit an event for the browser to speak
        socketio.emit('speak_alert', {'message': clean_text}, to=sid)

    emit_and_speak('nav_update', "Starting navigation process.")
    
    with client['location_lock']:
        start_location = client['current_location']
    
    if not start_location:
        emit_and_speak('nav_error', "Could not get your current location to start navigation.")
        return
            
    directions_data, error = get_directions(start_location, destination)
    if error or not directions_data or directions_data['status'] != 'OK':
        emit_and_speak('nav_error', f"Could not find a route. Reason: {error or directions_data.get('status', 'Unknown')}")
        return

    emit_and_speak('nav_update', "Route found! Starting guidance.")
    
    try:
        route = directions_data['routes'][0]
        leg = route['legs'][0]
        
        socketio.emit('nav_route_data', {'polyline': route['overview_polyline']['points']}, to=sid)
        emit_and_speak('nav_route_summary', f"Route to {route['summary']}. Total distance: {leg['distance']['text']}.")

        current_step_index = 0
        while client.get('is_gps_running') and current_step_index < len(leg['steps']):
            step = leg['steps'][current_step_index]
            step_end_location = (step['end_location']['lat'], step['end_location']['lng'])
            
            instruction = step['html_instructions']
            distance_text = step['distance']['text']
            emit_and_speak('nav_step', f"Step {current_step_index + 1}: {instruction} ({distance_text})", speak_text=f"In {distance_text}, {instruction}")

            while client.get('is_gps_running'):
                with client['location_lock']:
                    user_location = client['current_location']
                
                if not user_location:
                    time.sleep(1)
                    continue

                distance_to_waypoint = haversine_distance(user_location, step_end_location)
                socketio.emit('nav_distance_update', {'message': f"{distance_to_waypoint:.0f}m to next turn"}, to=sid)

                if distance_to_waypoint < WAYPOINT_PROXIMITY_THRESHOLD:
                    current_step_index += 1
                    break 
                time.sleep(1)
            
        if client.get('is_gps_running'):
            emit_and_speak('nav_complete', "You have arrived at your destination.")
    except (IndexError, KeyError) as e:
        emit_and_speak('nav_error', f"Error parsing the route directions: {e}")