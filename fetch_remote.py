import paramiko
import os

HOST = '23.140.140.70'
PASSWORD = '33nKDS80L6ipmedJ3y0j'
USER = 'root'
PORT = 22
REMOTE_LOG = '/opt/codex-console/logs/app.log'

print(f"Connecting to {HOST}...")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=10)
    print("Fetching logs...")
    stdin, stdout, stderr = ssh.exec_command(f'tail -n 250 {REMOTE_LOG}')
    lines = stdout.readlines()
    with open('remote_app_temp.log', 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print(f"Saved {len(lines)} lines")
    ssh.close()
except Exception as e:
    print(f"Error: {e}")
