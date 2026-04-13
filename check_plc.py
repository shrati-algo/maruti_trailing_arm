import socket

HOST = "127.0.0.1"
PORT = 4555

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind((HOST, PORT))
server.listen(1)

print(f"Listening on {HOST}:{PORT}")

conn, addr = server.accept()
print("Connected by", addr)

with conn:
    while True:
        data = conn.recv(4096)
        if not data:
            break
        print("Received:", data.decode("utf-8", errors="ignore"))