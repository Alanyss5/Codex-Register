import paramiko
import sys

def fetch_logs(ip, password, lines=1000):
    print(f"\n--- Fetching logs from {ip} ---")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username='root', password=password, timeout=10)
        
        # Get the current time on the server to provide context
        stdin, stdout, stderr = ssh.exec_command('date')
        print(f"Server Time: {stdout.read().decode('utf-8').strip()}")
        
        # Fetch docker logs
        cmd = f"docker logs --tail {lines} codex-console-webui-1 2>&1 | grep -E 'WARN|ERROR|注册|about-you|login_flow_failed|cookie|token|callback'"
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode('utf-8').strip()
        
        if out:
            filename = f"logs_{ip}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"Saved {len(out.splitlines())} lines to {filename}")
        else:
            print("No matching logs found.")
        
        ssh.close()
    except Exception as e:
        print(f"Error checking {ip}: {e}")

fetch_logs('198.46.152.138', 's3IEn8ClaxA4H99Zv7', 5000)
fetch_logs('23.140.140.70', '33nKDS80L6ipmedJ3y0j', 5000)
