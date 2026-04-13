import socket
import json

HOST = "127.0.0.1"
PORT = 4545

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

server_socket.bind((HOST, PORT))
server_socket.listen(1)

print(f"Listening on {HOST}:{PORT}...")

connection, client_address = server_socket.accept()
print(f"Connection established with {client_address}")

try:
    while True:
        data = connection.recv(1024)

        if not data:
            print("Client disconnected")
            break

        # Print raw data for debugging
        print("Raw received:", data)

        message = data.decode("utf-8", errors="ignore").strip()
        print("Decoded message:", message)

        try:
            json_data = json.loads(message)
            print("Received JSON:", json_data)

        except json.JSONDecodeError as e:
            print("JSON decode error:", e)

finally:
    connection.close()
    server_socket.close()
    print("Connection closed.")