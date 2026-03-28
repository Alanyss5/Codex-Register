import os
import zipfile
import paramiko

HOST = '23.140.140.70'
PASSWORD = '33nKDS80L6ipmedJ3y0j'
PORT = 22
USER = 'root'
REMOTE_DIR = '/opt/codex-console'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ZIP_FILE = os.path.join(BASE_DIR, 'deploy.zip')
EXCLUDE_DIRS = {'.git', '.venv', 'data', 'logs', '__pycache__', '.pytest_cache', '.vscode', '.idea'}

def create_zip():
    print(f"Creating zip file {ZIP_FILE}...")
    with zipfile.ZipFile(ZIP_FILE, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in files:
                if file.endswith('.zip') or file.endswith('.pyc') or file == 'logs*.txt':
                    continue
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, BASE_DIR)
                zipf.write(file_path, arcname)
    print("Zip created successfully.")

def deploy():
    print(f"\n--- Deploying to {HOST} ---")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=30, banner_timeout=30)
    print("Connected.")

    ssh.exec_command(f'mkdir -p {REMOTE_DIR}')
    ssh.exec_command(f'rm -rf {REMOTE_DIR}/logs/*.log')
    ssh.exec_command(f'rm -rf {REMOTE_DIR}/logs/*.txt')
    ssh.exec_command(f'mkdir -p {REMOTE_DIR}/data')
    ssh.exec_command(f'mkdir -p {REMOTE_DIR}/logs')

    print("Uploading zip file...")
    sftp = ssh.open_sftp()
    sftp.put(ZIP_FILE, f'{REMOTE_DIR}/deploy.zip')
    sftp.close()
    print("Upload complete.")

    print("Extracting and running docker-compose...")
    commands = [
        f"cd {REMOTE_DIR}",
        "unzip -o deploy.zip > /dev/null",
        "rm deploy.zip",
        "(docker-compose down || docker compose down || true)",
        "(docker-compose up -d --build || docker compose up -d --build)"
    ]
    command = ' && '.join(commands)
    stdin, stdout, stderr = ssh.exec_command(command, timeout=120)
    
    for line in stdout:
        print(line.strip())
    for line in stderr:
        print("ERR:", line.strip())
        
    exit_status = stdout.channel.recv_exit_status()
    if exit_status == 0:
        print(f"Deployment to {HOST} successful!")
    else:
        print(f"Deployment to {HOST} failed with exit status {exit_status}")
    
    ssh.close()

if __name__ == '__main__':
    create_zip()
    deploy()
    if os.path.exists(ZIP_FILE):
        os.remove(ZIP_FILE)
        print("Cleaned up local zip file.")
