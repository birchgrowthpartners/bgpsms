import subprocess
import csv
import os
import threading
import time
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify
from flask_socketio import SocketIO, emit
from db import fetch_table_rows, clear_table, add_contacts_to_tables, add_template_to_table, delete_template_from_table, reset_autoincrement, clear_templates

app = Flask(__name__)

# Set a secret key for flash messages
app.secret_key = 'your_secret_key_here'

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Path to the Python interpreter
PYTHON_PATH = "python3"  # Use system Python by default
MODEM_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modem_detection.py")

# Store detected modems
detected_modems = {
    'total': 0,
    'confirmed_ports': []
}

# Flag to track if detection is running
detection_running = False

# Store log messages for retrieval
log_messages = []

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/modems')
def modems():
    modems = fetch_table_rows('active_modems')
    return render_template('modems.html', table_data=modems, detected_modems=detected_modems)

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST' and 'csv_file' in request.files:
        file = request.files['csv_file']
        if file.filename.endswith('.csv'):
            try:
                # Read the uploaded CSV file and insert into the contacts_queue
                csv_data = csv.DictReader(file.stream)
                add_contacts_to_tables(csv_data)  # Add contacts only to the queue
                flash('Contacts uploaded successfully to the queue!')
            except Exception as e:
                flash(f"An error occurred while uploading the file: {e}")
        else:
            flash("Please upload a valid CSV file.")

    queue = fetch_table_rows('contacts_queue')
    sent = fetch_table_rows('contacts_sent')
    return render_template('upload.html', queue_data=queue, sent_data=sent)

@app.route('/inbox')
def inbox():
    replies = fetch_table_rows('contacts_replies')
    return render_template('inbox.html', replies_data=replies)

@app.route('/templates', methods=['GET', 'POST'])
def templates_page():
    try:
        if request.method == 'POST':
            template_text = request.form.get('template_text')
            if template_text:
                try:
                    add_template_to_table(template_text)
                    flash('Template added successfully!')
                except Exception as e:
                    flash(f"An error occurred while adding the template: {e}")
            else:
                flash("Template text cannot be empty.")
    
        templates = fetch_table_rows('templates')
        return render_template('templates.html', template_data=templates)
    except Exception as e:
        flash(f"An error occurred: {e}")
        # Return a simplified template or redirect if there's an error
        return redirect(url_for('home'))

@app.route('/delete-template/<int:template_id>')
def delete_template(template_id):
    try:
        delete_template_from_table(template_id)
        
        # Check if this was the last template, and if so, reset the autoincrement
        templates = fetch_table_rows('templates')
        if not templates:
            reset_autoincrement('templates')
            
        flash('Template deleted successfully!')
    except Exception as e:
        flash(f"An error occurred while deleting the template: {e}")
    return redirect(url_for('templates_page'))

@app.route('/clear-templates')
def clear_all_templates():
    try:
        clear_templates()  # This function both clears templates and resets the counter
        flash("All templates cleared and ID counter reset.")
    except Exception as e:
        flash(f"An error occurred: {e}")
    return redirect(url_for('templates_page'))

@app.route('/my-leads')
def my_leads():
    leads = fetch_table_rows('claimed_leads')
    return render_template('my_leads.html', leads_data=leads)

@app.route('/agents')
def agents():
    agent_rows = fetch_table_rows('agents')
    return render_template('agents.html', agent_data=agent_rows)

@app.route('/scraper')
def scraper():
    scraped = fetch_table_rows('scraper')
    return render_template('scraper.html', scraper_data=scraped)

@app.route('/logout')
def logout():
    return "Logged out (placeholder)"

# Parse the output from modem detection to extract the results
def parse_modem_detection_output(output):
    confirmed_ports = []
    total_modems = 0
    
    lines = output.strip().split('\n')
    
    # Find the line that mentions total confirmed working modems
    for i, line in enumerate(lines):
        if "Total confirmed working modems:" in line:
            try:
                total_modems = int(line.split(":")[1].strip())
                # Extract the confirmed ports from the following lines
                for j in range(i + 1, min(i + 1 + total_modems + 5, len(lines))):
                    if j < len(lines) and "Confirmed working port:" in lines[j]:
                        port = lines[j].split(":", 1)[1].strip()
                        confirmed_ports.append(port)
            except (ValueError, IndexError) as e:
                print(f"Error parsing output: {e}")
                
    return total_modems, confirmed_ports

# Route to get log messages
@app.route('/get-logs', methods=['GET'])
def get_logs():
    global log_messages
    return jsonify({"logs": log_messages})

# Route to start modem detection process
@app.route('/detect-ports', methods=['GET'])
def detect_ports():
    global detection_running, log_messages
    
    # Only start if not already running
    if detection_running:
        return jsonify({"status": "already_running"})
    
    # Reset log messages
    log_messages = ["Starting modem detection..."]
    
    detection_running = True
    
    # Reset detected modems
    global detected_modems
    detected_modems = {'total': 0, 'confirmed_ports': []}
    
    # Start the detection process in a background thread
    socketio.start_background_task(target=run_modem_detection)
    
    return jsonify({"status": "started"})

# Function to run the modem detection process
def run_modem_detection():
    global detection_running, log_messages
    
    try:
        # Signal that the process is starting
        socketio.emit('detection_status', {'status': 'started'})
        socketio.emit('new_output', {'data': "Starting modem detection..."})
        
        # Run the modem detection script
        process = subprocess.Popen(
            [PYTHON_PATH, MODEM_SCRIPT_PATH],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        
        # Initialize full output string
        full_output = ""
        
        # Stream stdout
        for stdout_line in iter(process.stdout.readline, ""):
            if stdout_line:
                line = stdout_line.strip()
                full_output += line + "\n"
                log_messages.append(line)
                socketio.emit('new_output', {'data': line})
                # Brief delay to allow frontend to process
                time.sleep(0.01)
        
        # Stream stderr
        for stderr_line in iter(process.stderr.readline, ""):
            if stderr_line:
                line = stderr_line.strip()
                error_line = "ERROR: " + line
                full_output += error_line + "\n"
                log_messages.append(error_line)
                socketio.emit('new_output', {'data': error_line})
                # Brief delay to allow frontend to process
                time.sleep(0.01)
        
        # Wait for process to complete and get return code
        return_code = process.wait()
        
        # Parse results only if the process completed successfully
        if return_code == 0:
            total_modems, confirmed_ports = parse_modem_detection_output(full_output)
            
            # Update the detected_modems global variable
            global detected_modems
            detected_modems = {
                'total': total_modems,
                'confirmed_ports': confirmed_ports
            }
            
            # Signal completion with the results
            socketio.emit('detection_complete', {
                'status': 'complete',
                'total': total_modems,
                'confirmed_ports': confirmed_ports
            })
            
            # Add completion message to the log
            complete_message = f"Detection complete! Found {total_modems} working modems."
            log_messages.append(complete_message)
            socketio.emit('new_output', {'data': complete_message})
        else:
            # Signal failure
            socketio.emit('detection_complete', {
                'status': 'failed',
                'error': f"Process exited with code {return_code}"
            })
            
            # Add failure message to the log
            fail_message = f"ERROR: Process failed with return code {return_code}"
            log_messages.append(fail_message)
            socketio.emit('new_output', {'data': fail_message})
    
    except Exception as e:
        error_message = f"Error in modem detection: {str(e)}"
        print(error_message)
        log_messages.append(error_message)
        socketio.emit('new_output', {'data': error_message})
        socketio.emit('detection_complete', {
            'status': 'error',
            'error': str(e)
        })
    
    finally:
        # Reset the running flag
        detection_running = False

# Route to check detection status
@app.route('/detection-status', methods=['GET'])
def check_detection_status():
    return jsonify({
        "running": detection_running,
        "detected_modems": detected_modems
    })

# Route to clear contacts from the queue
@app.route('/clear-queue')
def clear_queue():
    try:
        clear_table('contacts_queue')  # Clears the contacts queue
        flash("Contacts queue cleared.")
    except Exception as e:
        flash(f"An error occurred: {e}")
    return redirect(url_for('upload'))

# Route to clear contacts from the sent table
@app.route('/clear-sent')
def clear_sent():
    try:
        clear_table('contacts_sent')  # Clears the contacts sent table
        flash("Contacts sent table cleared.")
    except Exception as e:
        flash(f"An error occurred: {e}")
    return redirect(url_for('upload'))

# WebSocket handler for real-time updates
@socketio.on('connect')
def handle_connect():
    print("Client connected")
    # Send existing logs to newly connected clients
    if log_messages:
        emit('load_logs', {'logs': log_messages})

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
