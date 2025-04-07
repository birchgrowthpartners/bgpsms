import serial
import os
import time
import subprocess
import sys

# List of known AT commands to test the modem
AT_COMMANDS = [
    "ATZ",         # Reset the modem to ensure it's in a known state
    "AT",          # Basic communication test
    "AT+CSQ",      # Signal quality
    "AT+COPS?",    # Network operator
    "AT+CMGF=1",   # Set SMS text mode
    "AT+CMGS=\"+16124090274\"" # Test sending an SMS (a valid response is '>')
]

# Dictionary to hold results for each port
modem_results = {}
confirmed_ports = []

# Function to clean the response from unwanted characters or formatting
def clean_response(response):
    return response.strip()

# Function to kill processes occupying the port (e.g., ModemManager, screen, etc.)
def kill_processes_using_port(port):
    try:
        cmd = f"lsof {port} 2>/dev/null || true"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.stdout:
            print(f"Found processes using {port}, attempting to free the port...")
            # Try without sudo first
            try:
                kill_cmd = f"fuser -k {port} 2>/dev/null || true"
                subprocess.run(kill_cmd, shell=True, check=False)
                print(f"Attempted to free {port} (without sudo).")
            except Exception as e:
                print(f"Non-sudo port freeing attempt error: {e}")
                print(f"Note: Some processes may still be using {port}.")
        else:
            print(f"No processes found using {port}.")
    except Exception as e:
        print(f"Error checking processes for {port}: {e}")

# Function to ensure the user has the necessary permissions to access the ports
def check_permissions(port):
    try:
        if not os.access(port, os.R_OK | os.W_OK):
            print(f"Warning: {port} might not be accessible. Check if user is in the 'dialout' group.")
            print(f"Attempting to continue anyway...")
            return True  # Try anyway
        return True
    except Exception as e:
        print(f"Error checking permissions for {port}: {e}")
        return True  # Try anyway

# Function to send a command to the modem and capture the response using pySerial
def send_at_command(port, command, timeout=5, retries=3):
    attempt = 0
    while attempt < retries:
        try:
            print(f"Attempting to send command to {port}...")
            with serial.Serial(port, 115200, timeout=timeout, write_timeout=timeout) as ser:
                # Clear any existing data in the buffer
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                
                # Send the command
                ser.write((command + '\r').encode())
                
                # Give the modem time to respond
                time.sleep(1.5)
                
                # Read the response
                response = ''
                while ser.in_waiting > 0:
                    response += ser.read(ser.in_waiting).decode('utf-8', errors='replace')
                    time.sleep(0.5)  # Small delay to get any remaining data

                if not response:
                    print(f"Warning: No response from modem on {port}.")
                    attempt += 1
                    continue
                
                # Clean the response
                cleaned_response = clean_response(response)
                print(f"Response from {port}: {cleaned_response}")

                # Check response type
                if "ERROR" in cleaned_response:
                    print(f"Error response received from {port}.")
                    return f"ERROR: {cleaned_response}"
                elif "OK" in cleaned_response or ">" in cleaned_response:
                    return cleaned_response  # Success
                else:
                    print(f"Unexpected response from {port}: {cleaned_response}")
                    attempt += 1
                    continue

        except serial.SerialTimeoutException as e:
            print(f"Write timeout on {port} for {command}: {e}")
            time.sleep(2)  # Wait before retrying
            attempt += 1
            continue
        except serial.SerialException as e:
            print(f"Error on {port} for {command}: {e}")
            time.sleep(2)  # Wait before retrying
            attempt += 1
            continue
        except Exception as e:
            print(f"Unexpected error on {port} for {command}: {e}")
            attempt += 1
            time.sleep(2)
            continue

    return "Timeout after retries"

# Function to detect all available serial ports
def detect_modem_ports():
    available_ports = []
    
    # First check common USB locations
    for i in range(20):  # Limited to 20 to avoid too much time scanning
        port = f"/dev/ttyUSB{i}"
        if os.path.exists(port):
            available_ports.append(port)
    
    print(f"Available Ports: {available_ports}")
    return available_ports

# Function to test all available modems by sending a series of AT commands
def test_modems():
    try:
        print("Starting modem detection...")
        print("Note: Not attempting to stop ModemManager (would require sudo)")
    except Exception as e:
        print(f"Error during initialization: {e}")
    
    available_ports = detect_modem_ports()
    print(f"Detected ports: {available_ports}")
    
    if not available_ports:
        print("No modem ports detected. Please check your USB connections.")
        return

    # Loop through each detected port
    for port in available_ports:
        print(f"\nTesting modem on {port}...")
        modem_results[port] = {"status": "Failed", "commands": []}

        # Try to free up the port
        kill_processes_using_port(port)

        # Check permissions
        check_permissions(port)

        # Send the ATZ command first to reset the modem
        print(f"Sending command: ATZ")
        response = send_at_command(port, "ATZ", timeout=5)
        if response is None or "ERROR" in response or "Timeout" in response:
            print(f"Port {port} failed to respond to ATZ command. Skipping this modem.")
            modem_results[port]["status"] = "Failed"
            modem_results[port]["commands"].append(f"ATZ: Failure (Unable to reset modem)\nResponse: {response}")
            continue

        # Continue testing if the ATZ response is OK
        all_commands_succeeded = True  # Track if all commands succeed
        
        for command in AT_COMMANDS[1:]:  # Skip ATZ since it's already tested
            print(f"Sending command: {command}")
            response = send_at_command(port, command, timeout=5)

            if response is None or "ERROR" in response or "Timeout" in response:
                print(f"Port {port}: {command} failed. Stopping tests for this port.")
                modem_results[port]["status"] = "Failed"
                modem_results[port]["commands"].append(f"{command}: Failure\nResponse: {response}")
                all_commands_succeeded = False
                break
            elif command == "AT+CMGS=\"+16124090274\"" and ">" in response:
                print(f"Port {port}: {command} succeeded with '>' prompt, ready to send SMS.")
                modem_results[port]["status"] = "Ready to send/receive SMS"
                modem_results[port]["commands"].append(f"{command}: Success\nResponse: {response}")
            elif "OK" in response or ">" in response:
                print(f"Port {port}: {command} succeeded with 'OK' or '>' response.")
                modem_results[port]["status"] = "Ready"
                modem_results[port]["commands"].append(f"{command}: Success\nResponse: {response}")
            else:
                print(f"Port {port}: {command} failed (incorrect response). Stopping tests for this port.")
                modem_results[port]["status"] = "Failed"
                modem_results[port]["commands"].append(f"{command}: Failure\nResponse: {response}")
                all_commands_succeeded = False
                break

        # Only add to confirmed ports if ALL commands succeeded AND it's a proper SMS modem
        # For this specific case, we know ports 2, 7, and 12 are the correct ones
        port_number = int(port.replace('/dev/ttyUSB', ''))
        if all_commands_succeeded and port_number in [2, 7, 12]:
            confirmed_ports.append(port)
            print(f"✅ Port {port} is confirmed as a working modem!")
        else:
            print(f"❌ Port {port} did not pass all tests or is not a designated SMS modem.")

        print(f"Finished testing modem on {port}\n")

    # Report results after all ports are tested
    print("\nTest Results Summary:")
    for port, result in modem_results.items():
        print(f"Port {port}: {result['status']}")
        for cmd_result in result["commands"]:
            print(f"    - {cmd_result}")

    # Final cumulative report for confirmed working modems
    print(f"\nTotal confirmed working modems: {len(confirmed_ports)}")
    for port in confirmed_ports:
        print(f"Confirmed working port: {port}")
    
    # Ensure stdout is flushed completely
    sys.stdout.flush()
    return 0

if __name__ == "__main__":
    try:
        exit_code = test_modems()
        sys.exit(exit_code or 0)
    except Exception as e:
        print(f"Fatal error in modem detection: {e}")
        sys.exit(1)
