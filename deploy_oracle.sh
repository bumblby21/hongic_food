#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="$(whoami)"

echo "=== [1/5] 시스템 패키지 및 한국어 OCR(Tesseract) 설치 ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-kor nginx

echo "=== [2/5] Python 가상환경 및 라이브러리 설치 ==="
cd "$APP_DIR"
python3 -m venv .venv
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r requirements.txt

echo "=== [3/5] 운영용 .env 보안키 생성 ==="
if [ ! -f "$APP_DIR/.env" ]; then
  SECRET_VAL="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  PII_VAL="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  read -r -p "관리자(admin) 계정에 사용할 새 비밀번호를 입력하세요 (엔터 시 admin1234): " ADMIN_PW
  ADMIN_PW="${ADMIN_PW:-admin1234}"
  cat > "$APP_DIR/.env" <<EOF
HONGCHELIN_SECRET=${SECRET_VAL}
HONGCHELIN_PII_KEY=${PII_VAL}
HONGCHELIN_ADMIN_PASSWORD=${ADMIN_PW}
HONGCHELIN_HTTPS=0
PORT=3000
EOF
  chmod 600 "$APP_DIR/.env"
  echo ".env 파일 생성 완료 (권한 600)"
else
  echo "기존 .env 파일을 유지합니다."
fi

echo "=== [4/5] systemd 백그라운드 서비스 등록 (24시간 자동 실행) ==="
sudo tee /etc/systemd/system/hongchelin.service > /dev/null <<EOF
[Unit]
Description=Hongchelin Guide Flask App
After=network.target

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/gunicorn server:app --bind 127.0.0.1:3000 --workers 2 --timeout 60
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable hongchelin
sudo systemctl restart hongchelin

echo "=== [5/5] Nginx 리버스 프록시 & 방화벽 포트(80/443) 설정 ==="
sudo tee /etc/nginx/sites-available/hongchelin > /dev/null <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 15M;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/hongchelin /etc/nginx/sites-enabled/hongchelin
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

# Oracle Ubuntu 기본 iptables 80/443 허용 (있는 경우)
if command -v iptables >/dev/null 2>&1; then
  sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
  sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
  if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save || true
  fi
fi

echo ""
echo "✅ 설치 완료! 서비스 상태 확인:"
sudo systemctl status hongchelin --no-pager -l | head -n 12
echo ""
echo "👉 Oracle Cloud 콘솔 [VCN > Security List > Ingress Rules]에서 TCP 80, 443 포트를 열어두었다면"
echo "   이제 http://<서버_공인_IP> 로 누구나 접속할 수 있습니다!"
