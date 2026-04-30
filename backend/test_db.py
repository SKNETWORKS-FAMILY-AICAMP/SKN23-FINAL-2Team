"""
File    : backend/test_db.py
Author  : 김민정
WBS     : TEST-01
Create  : 2026-04-07
Description : 데이터베이스 및 SSH 터널링 연결 확인 스크립트

Modification History :
    - 2026-04-07 (김민정) : 초기 생성 및 DB 연결 테스트 로직 구현
"""
import os
import socket
import psycopg2
from sshtunnel import SSHTunnelForwarder

def parse_env():
    env_vars = {}
    try:
        # utf-8-sig로 읽어서 BOM 제거
        with open(".env", "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    except Exception as e:
        print(f"Warning: Could not read .env file: {e}")
    return env_vars

def check_db_connection():
    env = parse_env()
    print(f"Loaded Keys: {list(env.keys())}")
    
    # DB 정보
    db_host = env.get("DB_HOST")
    db_port = int(env.get("DB_PORT", 5432))
    db_user = env.get("DB_USER")
    db_pass = env.get("DB_PASSWORD")
    db_name = env.get("DB_NAME")
    
    # SSH 정보
    use_ssh = env.get("USE_SSH_TUNNEL", "False").lower() == "true"
    ssh_host = env.get("SSH_HOST")
    ssh_port = int(env.get("SSH_PORT", 22))
    ssh_user = env.get("SSH_USER")
    ssh_key = env.get("SSH_KEY_PATH")

    print(f"\n--- Unified DB Connection Test ---")
    print(f"Use SSH Tunnel: {use_ssh}")

    if not db_host or not db_user or (use_ssh and not ssh_host):
        print("[FAIL] Missing required credentials.")
        return

    tunnel = None
    target_host = db_host
    target_port = db_port

    if use_ssh:
        print("\n[Step 1] Creating SSH Tunnel...")
        try:
            tunnel = SSHTunnelForwarder(
                (ssh_host, ssh_port),
                ssh_username=ssh_user,
                ssh_pkey=ssh_key,
                remote_bind_address=(db_host, db_port),
                local_bind_address=('127.0.0.1', 0)
            )
            tunnel.start()
            target_host = '127.0.0.1'
            target_port = tunnel.local_bind_port
            print(f"[SUCCESS] SSH Tunnel active (Local Port: {target_port})")
        except Exception as e:
            print(f"[FAIL] SSH Tunnel error: {e}")
            return

    # 2. Network Check
    print(f"\n[Step 2] Connectivity Check ({target_host}:{target_port})")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((target_host, target_port))
        if result == 0:
            print(f"[SUCCESS] Port {target_port} is reachable.")
        else:
            print(f"[FAIL] Port {target_port} unreachable (Code: {result})")
        sock.close()
    except Exception as e:
        print(f"[FAIL] Network check error: {e}")

    # 3. DB Check
    print("\n[Step 3] DB Connection Attempt (psycopg2)")
    try:
        conn = psycopg2.connect(
            host=target_host,
            port=target_port,
            user=db_user,
            password=db_pass,
            database=db_name,
            connect_timeout=10
        )
        print("[SUCCESS] Connected to DATABASE successfully!!!")
        conn.close()
    except Exception as e:
        print(f"[FAIL] DB connection error: {e}")
    finally:
        if tunnel:
            tunnel.stop()
            print("\nSSH Tunnel closed.")

if __name__ == "__main__":
    check_db_connection()