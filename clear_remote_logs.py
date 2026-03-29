import paramiko

HOST = '23.140.140.70'
PASSWORD = '33nKDS80L6ipmedJ3y0j'
USER = 'root'
PORT = 22

print(f"Connecting to {HOST} to clear logs...")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=10)
    # Use truncate so we don't break the active file descriptor in docker
    stdin, stdout, stderr = ssh.exec_command('truncate -s 0 /opt/codex-console/logs/*.log')
    print("Remote logs have been truncated successfully.")
    ssh.close()
except Exception as e:
    print(f"Error connecting or clearing logs: {e}")
