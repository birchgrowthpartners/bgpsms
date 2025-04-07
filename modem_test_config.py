import serial
import os
import time
import subprocess
import sqlite3
import threading
import queue
import signal
import sys
from flask_socketio import SocketIO

# Global variables
test_results = {}
port_statuses = {}
response_queues = {}
stop_listeners = {}
socketio = None
confirmed_working_ports = []

# Function to initialize SocketIO (will be set from app.py)
def set_socketio(socket_instance):
    global socketio
    socketio = socket_instance

# Function to clean the response from unwanted characters or formatting
def clean_response(response):
    return response.strip()

# Function to kill processes occupying the port
def kill_processes_using_port(port):
    try:
        cmd = f"lsof {port} 2>/dev/null"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.stdout:
            # Use fuser without sudo - it will only kill processes the current user has permission for
            kill_cmd = f"fuser -k {port} 2>/dev/null"
            subprocess.run(kill_cmd, shell=True, check=False)
            log_message(f"Killed processes using {port}.")
        else:
            log_message(f"No processes found using {port}.")
    except subprocess.CalledProcessError as e:
        log_message(f"Error while killing processes for {port}: {e}")
    except Exception as e:
        log_message(f"Unexpected error while killing processes for {port}: {e}")

# Function to stop ModemManager service (if it's running)
def stop_modem_manager():
    try:
        log_message("Checking ModemManager status...")
        # Check if ModemManager is running and if user has permission to manage it
        check_cmd = "systemctl is-active ModemManager 2>/dev/null || echo 'inactive'"
        result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)
        
        if result.stdout.strip() == 'active':
            log_message("ModemManager is active. Attempting to stop without sudo...")
            # Try stopping without sudo first
            try:
                subprocess.run("systemctl --user stop ModemManager", shell=True, check=True, timeout=5)
                log_message("ModemManager stopped successfully without sudo.")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                log_message("Could not stop ModemManager without sudo. Proceeding anyway.")
                log_message("Note: You may need to manually stop ModemManager with 'sudo systemctl stop ModemManager'")
        else:
            log_message("ModemManager is not active or not installed.")
            
    except Exception as e:
        log_message(f"Error with ModemManager check: {e}. Proceeding anyway.")

# Function to ensure the user has the necessary permissions to access the ports
def check_permissions(port):
    try:
        if not os.access(port, os.R_OK | os.W_OK):
            log_message(f"Error: {port} is not accessible. Ensure the user is in the 'dialout' group.")
            log_message(f"Tip: Run 'sudo usermod -a -G dialout $USER' and then log out and back in.")
            return False
        return True
    except Exception as e:
        log_message(f"Error checking permissions for {port}: {e}")
        return False

# Function to send a command to the modem
def send_at_command(port, command, timeout=5, retries=3):
    attempt = 0
    while attempt < retries:
        try:
            log_message(f"Attempting to send command to {port}: {command}")
            with serial.Serial(port, 115200, timeout=timeout, write_timeout=timeout) as ser:
                ser.flush()  # Ensure the port is clear before sending
                ser.write((command + '\r').encode())  # Send command with carriage return

                # Read the response from the modem
                time.sleep(2)  # Give the modem more time to respond
                response = ser.read(ser.in_waiting).decode('utf-8', errors='replace')  # Read all available data

                if not response:
                    log_message(f"Warning: No response from modem on {port}.")
                    
                # Clean the response to remove unwanted lines
                cleaned_response = clean_response(response)
                log_message(f"Response from {port}: {cleaned_response}")

                # Checking if the response indicates success or failure
                if "ERROR" in cleaned_response:
                    log_message(f"Error response received from {port}.")
                    return f"ERROR: {cleaned_response}"
                elif "OK" in cleaned_response or ">" in cleaned_response:
                    return cleaned_response  # Success
                else:
                    return cleaned_response  # Return whatever we received

        except serial.SerialTimeoutException as e:
            log_message(f"Write timeout on {port} for {command}: {e}")
            time.sleep(3)  # Wait before retrying
            attempt += 1
            continue
        except serial.SerialException as e:
            log_message(f"Error on {port} for {command}: {e}")
            time.sleep(3)  # Wait before retrying
            attempt += 1
            continue
        except KeyboardInterrupt:
            log_message(f"Process interrupted while testing {port}. Exiting...")
            raise
        except Exception as e:
            log_message(f"Unexpected error on {port} for {command}: {e}")
            return f"Error: {e}"

    return "Timeout after retries"

# Function to send SMS message
def send_sms(port, phone_number, message):
    try:
        port_statuses[port] = "Sending..."
        emit_port_status(port)
        
        # Set SMS text mode
        text_mode_response = send_at_command(port, "AT+CMGF=1")
        if "ERROR" in text_mode_response:
            log_message(f"Failed to set SMS text mode on {port}")
            port_statuses[port] = "Failed to send"
            emit_port_status(port)
            return False
        
        # Send message command
        send_cmd_response = send_at_command(port, f'AT+CMGS="{phone_number}"')
        if ">" not in send_cmd_response:
            log_message(f"Modem did not respond with prompt on {port}")
            port_statuses[port] = "Failed to send"
            emit_port_status(port)
            return False
        
        # Send the message content followed by CTRL+Z (ASCII 26)
        with serial.Serial(port, 115200, timeout=10, write_timeout=10) as ser:
            ser.write((message + chr(26)).encode())
            time.sleep(5)  # Give more time for sending
            response = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
            
        if "OK" in response:
            log_message(f"SMS sent successfully from {port} to {phone_number}")
            port_statuses[port] = "Message sent"
            emit_port_status(port)
            
            # Store the test result in the database
            store_sms_test_result(port, phone_number, message, True, response)
            return True
        else:
            log_message(f"Failed to send SMS from {port}: {response}")
            port_statuses[port] = "Failed to send"
            emit_port_status(port)
            
            # Store the test result in the database
            store_sms_test_result(port, phone_number, message, False, response)
            return False
            
    except Exception as e:
        log_message(f"Error sending SMS from {port}: {e}")
        port_statuses[port] = "Error"
        emit_port_status(port)
        
        # Store the test result in the database
        store_sms_test_result(port, phone_number, message, False, str(e))
        return False

# Function to continuously listen for incoming SMS on a specific port
def listen_for_sms(port):
    log_message(f"Starting SMS listener on {port}")
    response_queues[port] = queue.Queue()
    stop_listeners[port] = threading.Event()
    
    # Set up serial connection to the modem
    try:
        # Configure modem for SMS reception
        send_at_command(port, "AT+CMGF=1")  # Set to text mode
        send_at_command(port, "AT+CNMI=2,2,0,0,0")  # Configure how new messages are handled
        
        # Start continuous monitoring loop
        with serial.Serial(port, 115200, timeout=1) as ser:
            log_message(f"Listener active on {port}")
            port_statuses[port] = "Listening"
            emit_port_status(port)
            
            while not stop_listeners[port].is_set():
                try:
                    if ser.in_waiting > 0:
                        data = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
                        
                        # Check for incoming SMS notification
                        if "+CMT:" in data:
                            log_message(f"Incoming SMS detected on {port}: {data}")
                            # Extract sender and message content
                            lines = data.strip().split('\n')
                            sender_line = next((line for line in lines if "+CMT:" in line), "")
                            sender = sender_line.split('"')[1] if '"' in sender_line else "Unknown"
                            
                            # The message content is typically in the line after the CMT notification
                            message_start_index = lines.index(sender_line) + 1 if sender_line in lines else -1
                            message_content = lines[message_start_index].strip() if message_start_index < len(lines) and message_start_index >= 0 else "Empty message"
                            
                            # Add the message to the queue
                            message_info = {
                                "sender": sender,
                                "content": message_content,
                                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                            }
                            response_queues[port].put(message_info)
                            
                            # Emit the message to the web UI
                            emit_new_response(port, message_info)
                            
                            # Store the message in the database
                            store_received_sms(port, sender, message_content)
                            
                            port_statuses[port] = "Message received"
                            emit_port_status(port)
                        
                    time.sleep(0.5)  # Short sleep to prevent CPU overuse
                except Exception as e:
                    log_message(f"Error in SMS listener for {port}: {e}")
                    time.sleep(1)  # Longer sleep after error
                    
        log_message(f"SMS listener stopped for {port}")
        port_statuses[port] = "Listener stopped"
        emit_port_status(port)
        
    except Exception as e:
        log_message(f"Failed to start SMS listener on {port}: {e}")
        port_statuses[port] = "Listener error"
        emit_port_status(port)

# Function to stop the SMS listener for a specific port
def stop_sms_listener(port):
    if port in stop_listeners:
        log_message(f"Stopping SMS listener for {port}")
        stop_listeners[port].set()
        time.sleep(2)  # Give the listener thread time to stop
        port_statuses[port] = "Ready"
        emit_port_status(port)
        return True
    return False

# Function to log messages (both to console and through SocketIO)
def log_message(message):
    print(message)  # Console output for debugging
    if socketio:
        socketio.emit('config_output', {'data': message})

# Function to emit port status updates
def emit_port_status(port):
    if socketio:
        socketio.emit('port_status_update', {
            'port': port,
            'status': port_statuses.get(port, "Unknown")
        })

# Function to emit new SMS responses
def emit_new_response(port, message_info):
    if socketio:
        socketio.emit('new_sms_response', {
            'port': port,
            'sender': message_info['sender'],
            'content': message_info['content'],
            'timestamp': message_info['timestamp']
        })

# Function to get DB connection
def get_db_connection():
    conn = sqlite3.connect('bgpsms.db')
    conn.row_factory = sqlite3.Row
    return conn

# Function to store SMS test results
def store_sms_test_result(port, phone_number, message, success, response=None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            INSERT INTO modem_test_log 
            (port, phone_number, message, success, response, timestamp) 
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (port, phone_number, message, 1 if success else 0, response)
        )
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log_message(f"Database error storing SMS test result: {e}")
        return False

# Function to store received SMS messages
def store_received_sms(port, sender, message):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if the contact exists in any of our tables
        contact = cursor.execute(
            """
            SELECT name FROM contacts_queue WHERE phone = ?
            UNION
            SELECT name FROM contacts_sent WHERE phone = ?
            UNION
            SELECT name FROM claimed_leads WHERE phone = ?
            """,
            (sender, sender, sender)
        ).fetchone()
        
        name = contact['name'] if contact else "Unknown"
        
        # Insert the reply
        cursor.execute(
            """
            INSERT INTO contacts_replies 
            (name, phone, message, port, timestamp) 
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (name, sender, message, port)
        )
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log_message(f"Database error storing received SMS: {e}")
        return False

# Function to update the active_modems table in the database
def update_modem_database(ports, status="Ready"):
    try:
        # Connect to the database
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Only update the database with fully confirmed working ports
        for port in ports:
            # Check if port already exists
            existing = cursor.execute('SELECT id FROM active_modems WHERE port = ?', (port,)).fetchone()
            
            if existing:
                # Update existing entry
                cursor.execute('UPDATE active_modems SET status = ? WHERE port = ?', (status, port))
            else:
                # Insert new entry
                cursor.execute('INSERT INTO active_modems (port, status) VALUES (?, ?)', (port, status))
        
        # Commit changes and close connection
        conn.commit()
        conn.close()
        log_message(f"Updated database with {len(ports)} active modems")
        return True
        
    except Exception as e:
        log_message(f"Database update error: {e}")
        return False

# Main function to initialize and test all modems
def init_modem_testing(ports_to_test):
    global confirmed_working_ports
    
    try:
        log_message("Starting modem testing and configuration...")
        
        # Stop ModemManager to free up ports
        stop_modem_manager()
        
        confirmed_working_ports = []
        
        # Initialize status for each port
        for port in ports_to_test:
            port_statuses[port] = "Initializing"
            emit_port_status(port)
            
            # Kill any processes using the port
            kill_processes_using_port(port)
            
            # Ensure the port is accessible
            if not check_permissions(port):
                port_statuses[port] = "Permission Denied"
                emit_port_status(port)
                continue
            
            # Initialize the modem
            reset_response = send_at_command(port, "ATZ")
            if "ERROR" in reset_response or "Timeout" in reset_response:
                log_message(f"Failed to reset modem on {port}")
                port_statuses[port] = "Failed to Initialize"
                emit_port_status(port)
                continue
                
            # Test basic AT command
            test_response = send_at_command(port, "AT")
            if "OK" not in test_response:
                log_message(f"Basic AT command failed on {port}")
                port_statuses[port] = "Failed"
                emit_port_status(port)
                continue
                
            # Set Text Mode for SMS
            sms_mode_response = send_at_command(port, "AT+CMGF=1")
            if "OK" not in sms_mode_response:
                log_message(f"Failed to set SMS text mode on {port}")
                port_statuses[port] = "SMS Mode Failed"
                emit_port_status(port)
                continue
            
            # Test SMS sending capability (just check if it responds with prompt)
            sms_test_response = send_at_command(port, "AT+CMGS=\"+1234567890\"")
            if ">" not in sms_test_response:
                log_message(f"Modem on {port} does not support SMS sending")
                port_statuses[port] = "SMS Not Supported"
                emit_port_status(port)
                continue
                
            # Send escape sequence to cancel the SMS (CTRL+Z)
            with serial.Serial(port, 115200, timeout=5) as ser:
                ser.write(chr(27).encode())  # ESC to cancel
                time.sleep(1)
                ser.read(ser.in_waiting)  # Clear buffer
                
            # Configuration successful
            log_message(f"Modem on {port} initialized and ready for SMS operations")
            port_statuses[port] = "Ready"
            emit_port_status(port)
            
            # Add to confirmed working ports
            confirmed_working_ports.append(port)
            
        log_message("Modem testing and configuration completed.")
        log_message(f"Total confirmed working modems: {len(confirmed_working_ports)}")
        
        # Update database with confirmed ports only after successful configuration
        if confirmed_working_ports:
            update_modem_database(confirmed_working_ports)
            
        return confirmed_working_ports
        
    except Exception as e:
        log_message(f"Error in modem testing initialization: {e}")
        return []

# Function to start listening for incoming SMS on all confirmed ports
def start_all_listeners(confirmed_ports):
    log_message(f"Starting SMS listeners on {len(confirmed_ports)} ports")
    for port in confirmed_ports:
        # Start each listener in a separate thread
        thread = threading.Thread(target=listen_for_sms, args=(port,))
        thread.daemon = True  # Daemon threads will be killed when main thread exits
        thread.start()

# Function to stop all SMS listeners
def stop_all_listeners():
    for port in list(stop_listeners.keys()):
        stop_sms_listener(port)
    log_message("All SMS listeners stopped")

# Signal handler for graceful shutdown
def signal_handler(sig, frame):
    log_message("Shutdown signal received. Stopping all listeners...")
    stop_all_listeners()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Main entry point (called from app.py)
def main(ports_to_test):
    try:
        # Initialize database tables if not exist
        init_database_tables()
        
        # Initialize and test all modems
        confirmed_ports = init_modem_testing(ports_to_test)
        
        if not confirmed_ports:
            log_message("No modems were successfully configured. Please check connections and permissions.")
            return False
            
        # Start SMS listeners on all ports
        # Commenting this out - we'll let the user start listeners manually
        # start_all_listeners(confirmed_ports)
        
        return True
    except Exception as e:
        log_message(f"Error in modem_test_config main function: {e}")
        return False

# Function to ensure necessary database tables exist
def init_database_tables():
    try:
        conn = sqlite3.connect('bgpsms.db')
        cursor = conn.cursor()
        
        # Create modem_test_log table if it doesn't exist
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS modem_test_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            port TEXT NOT NULL,
            phone_number TEXT NOT NULL,
            message TEXT NOT NULL,
            success INTEGER NOT NULL,
            response TEXT,
            timestamp TEXT NOT NULL
        )
        ''')
        
        # Create active_modems table if it doesn't exist
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_modems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            port TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL
        )
        ''')
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        log_message(f"Error initializing database tables: {e}")
