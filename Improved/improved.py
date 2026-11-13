# Her skriver vi vores egne version af bully algoritmen ud fra slides beskrivelsen.

import os
import socket
import threading
import requests
import time
from flask import Flask, jsonify, request


app = Flask(__name__)

SERVICE_NAME = "bully-service"
PORT = 5000
POD_NAME = os.getenv('POD_NAME')
leader_id = None



def get_my_id() -> str:
    if not POD_NAME:
        raise ValueError("POD_NAME not found")
    else:
        POD_ID = int(POD_NAME.split("-")[-1])
        return POD_ID



def heart_beat():
    time.sleep(3)
    interval = 2
    my_id = get_my_id()

    while True:
        time.sleep(interval)
        if not is_peer_ready(leader_id, "HEARTBEAT"):
            print(f"[LEADER OFFLINE] leader id {leader_id}. {my_id} starting new election")
            start_election()


def peer_url(peer_id: int) -> str: 
    # K8s Headless Service DNS resolution:
    return f"http://bully-{peer_id}.{SERVICE_NAME}:{PORT}"

def log_msg(kind, peer_id):
    # One line per message sent; measure.py counts these
    print(f"[MSG] {kind} {get_my_id()}->{peer_id}")

def is_peer_ready(peer_id: int, kind: str) -> bool:
    log_msg(kind, peer_id)
    url = f"{peer_url(peer_id)}/ready"
    try:
        response = requests.get(url, timeout=2)
        return response.status_code == 200  
    except:
        return False


election_lock = threading.Lock()

def start_election():
    # Kun ét valg ad gangen i denne pod; ekstra kald (heartbeat, /election) springes over
    if not election_lock.acquire(blocking=False):
        return
    try:
        run_election()
    finally:
        election_lock.release()

def run_election():
    my_id = get_my_id()
    last_id = my_id

    print(f'[STARTING ELECTION] pod id: {my_id}')

    # Find the highest pod id through DNS only (no messages sent)
    while True:
        try:
            socket.gethostbyname(f"bully-{last_id + 1}.bully-service")
        except socket.gaierror: # If pod doesnt exist break
            break
        last_id += 1

    # FIX 2: probe from the highest id downwards and stop at the first pod that answers,
    # instead of probing every higher pod (the pods below the first alive one are never probed)
    for peer_id in range(last_id, my_id, -1):
        if is_peer_ready(peer_id, "PROBE"):
            # FIX 1: pass the election to the highest alive pod, not the lowest.
            # It finds nobody alive above it and wins at once, instead of a chain where every pod re-probes all above it
            print(f'[ELECTION POD RESPONSE] {peer_id}')
            if pass_on_election(peer_id): # If the pod accepts responsibility we are done
                return

    # No higher pod alive (or none accepted), declare victory
    declare_victory(my_id)

def pass_on_election(peer_id):
    print(f'[PASSING ELECTION] to pod id: {peer_id}')
    log_msg("ELECTION", peer_id)
    url = f"{peer_url(peer_id)}/election"
    try:
        response = requests.post(url, timeout=2.0)
        response.raise_for_status()
        return True # Election responsibility accepted by new pod
    except requests.exceptions.RequestException:
        return False
        
def set_new_leader(new_leader_id):
    global leader_id 
    leader_id = new_leader_id
        
def declare_victory(new_leader_id):
    #send ud til alle at vinderen er kåret
    set_new_leader(new_leader_id)

    print(f'[DECLARING VICTORY] {time.strftime("%H:%M:%S")} leader id: {new_leader_id}')
    

    for peer_id in range(new_leader_id):
        # Next pod_id
        url = f"{peer_url(peer_id)}/victory"
        log_msg("COORDINATOR", peer_id)
        try:
            requests.post(url, json={'new_leader_id': new_leader_id}, timeout=2)
        except requests.RequestException:
            pass  # pod død, prøv næst/victory route needs to reade 
  
def on_startup():
    time.sleep(3)
    start_election()
    threading.Thread(target=heart_beat, daemon=True).start()

# END POINTS --------------------
victory_lock = threading.Lock()  # Flask runs each request in its own thread; victories arriving together must not race

@app.post('/victory')
def on_victory():
    new_leader_id = request.json["new_leader_id"]
    with victory_lock:
        if leader_id is not None and new_leader_id < leader_id and is_peer_ready(leader_id, "PROBE"):
            # Two nodes claim to lead (e.g. they started together); a new election lets the highest win
            print(f'[CONFLICTING VICTORY] {new_leader_id} claims to lead, but {leader_id} is alive')
            threading.Thread(target=start_election, daemon=True).start()
            return "ok", 200
        set_new_leader(new_leader_id)
        print(f'[NEW LEADER] {time.strftime("%H:%M:%S")} leader id: {leader_id}')
    return "ok", 200


@app.post("/election")
def on_election():
    # A lower node is holding an election: answer OK and take over.
    threading.Thread(target=start_election, daemon=True).start()
    return jsonify(ok=True)

@app.get("/ready")
def readiness_check():
    return "ok", 200



if __name__ == "__main__":
    print(f"[{POD_NAME}] Started") # BROADCAST
    # Elect in the background so the server is up first; otherwise a node starting at the same time can't reach us
    threading.Thread(target=on_startup, daemon=True).start()
    # Start web server on port 5000
    app.run(host="0.0.0.0", port=5000)



