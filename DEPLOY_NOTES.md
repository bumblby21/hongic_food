# 홍슐랭 가이드 운영 · 배포 요약 메모

다른 컴퓨터에서 작업을 이어갈 때 AI 에이전트 또는 본인 참고용으로 사용하는 운영 요약 문서입니다.
(보안상 비밀번호, SSH 개인키, `.env` 시크릿 키, rclone 토큰 등 민감 정보는 포함되어 있지 않습니다.)

## 1. 실서버 기본 정보
- **클라우드**: Oracle Cloud Always Free (Ubuntu 22.04, 1GB RAM + 2GB Swap)
- **공인 IP**: `168.110.34.255`
- **도메인 (HTTPS)**: `https://hongic-food-guide.duckdns.org` (DuckDNS + Let's Encrypt Certbot)
- **SSH 접속 계정**: `ubuntu@168.110.34.255`
- **SSH 키 파일**: 별도 안전한 경로에 보관 (예: `ssh-key-2026-11-16.key`, 절대 GitHub 등 공개 저장소에 업로드 금지)

## 2. 서버 내 디렉토리 및 서비스 구조
- **앱 경로**: `/home/ubuntu/hongchelin`
  - 주요 파일: `server.py`, `database.py`, `algorithm.py`, `index.html`, `manifest.json`, `sw.js`, `requirements.txt`, `deploy_oracle.sh`, `backup_to_gdrive.sh`
  - 데이터: `hongchelin.db` (SQLite), `uploads/` (사용자 업로드 리뷰/영수증 사진), `.env` (시크릿 키·관리자 비밀번호·HTTPS 설정)
- **WSGI 서비스 (`systemd`)**: `hongchelin.service`
  - Gunicorn (`--bind 127.0.0.1:3000 --workers 2 --timeout 60`, `Restart=always`)
  - 상태 확인 / 재시작: `sudo systemctl status hongchelin`, `sudo systemctl restart hongchelin`
  - 로그 확인: `sudo journalctl -u hongchelin -n 50 --no-pager`
- **웹 서버 / 리버스 프록시**: Nginx
  - 사이트 설정: `/etc/nginx/sites-available/hongchelin` (SSL은 Certbot 관리, `client_max_body_size 15M`)
  - 요청 제한(Rate Limit): `/etc/nginx/conf.d/hongchelin_ratelimit.conf`
    - `/api/auth/(login|register|delete-account)`: 분당 5회 (`burst=5`)
    - `/api/(upload|verify-receipt)`: 분당 10회 (`burst=5`)
    - 초과 시 `429 Too Many Requests`
  - 설정 검증 / 반영: `sudo nginx -t && sudo systemctl reload nginx`
- **SSL 인증서 자동 갱신**: `certbot.timer` (`systemd` 타이머로 자동 갱신)

## 3. 자동 백업 (Google Drive)
- **스크립트**: `/home/ubuntu/hongchelin/backup_to_gdrive.sh`
- **스케줄 (`crontab`)**: `0 19 * * *` UTC (한국시간 매일 새벽 04:00)
- **백업 방식**:
  - 실행 중인 `hongchelin.db`를 Python `sqlite3` `backup()`으로 안전 복제한 뒤 `uploads/` 및 `.env`와 함께 `.tar.gz` 압축
  - 서버 로컬 `/home/ubuntu/hongchelin_backups/`에 최근 7개 보관
  - `rclone` (`gdrive:hongchelin_backups`)으로 Google Drive 업로드 및 30일 이상 된 백업 정리

## 4. 코드 수정 후 실서버 반영 방법 (PowerShell 예시)
로컬에서 수정한 파일을 업로드한 뒤 `hongchelin` 서비스를 재시작합니다. (`index.html`, `manifest.json` 등 정적 파일만 바꾼 경우 재시작 없이 바로 반영되지만, `server.py` / `database.py` 변경 시 반드시 재시작 필요)

```powershell
# 파일 업로드 예시
scp -i "<SSH_KEY_PATH>" server.py database.py index.html ubuntu@168.110.34.255:~/hongchelin/

# 백엔드 서비스 재시작
ssh -i "<SSH_KEY_PATH>" ubuntu@168.110.34.255 "sudo systemctl restart hongchelin && systemctl is-active hongchelin"
```

## 5. 다른 컴퓨터에서 새 대화 시작 시 AI에게 전달할 한 줄 예시
> "이 프로젝트의 실서버는 `ubuntu@168.110.34.255` (`https://hongic-food-guide.duckdns.org`)이고 `DEPLOY_NOTES.md`를 참고해. SSH 키 경로는 `<경로>`야."
