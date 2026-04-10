# 📊 Discord 시장 시세 봇

미국/한국 대표 지수, 원유 선물, 미국/한국 단기·장기 국채 시세를 매일 정해진 시간에 디스코드 채널에 자동 게시하는 봇입니다.

## 📈 표시 종목

| 카테고리 | 종목 |
|---|---|
| **미국 지수** | S&P 500, 나스닥, 다우존스, 러셀 2000 |
| **한국 지수** | 코스피, 코스닥 |
| **원유 선물** | WTI, 브렌트유 |
| **미국 국채** | 단기 (SHY, 1-3년) / 장기 (TLT, 20년+) |
| **한국 국채** | 단기 (KOSEF 국고채3년) / 장기 (KODEX 국채선물10년) |

> 국채는 "가격 변동"을 보기 위해 yield(금리) 대신 대표 ETF의 가격 기준으로 표기합니다.

---

## 1. Discord 봇 만들기 (토큰 발급)

1. [Discord Developer Portal](https://discord.com/developers/applications) 접속 → 로그인
2. 우측 상단 **"New Application"** → 이름 입력 → Create
3. 좌측 메뉴 **"Bot"** → **"Add Bot"** → 확인
4. **"Reset Token"** 클릭 → 토큰 복사 (한 번만 표시되니 잘 보관)
5. 아래 권한 옵션은 기본값 그대로 두면 됩니다 (메시지 전송만 필요).

## 2. 봇을 서버에 초대

1. 좌측 **"OAuth2" → "URL Generator"** 메뉴 이동
2. **SCOPES**에서 `bot` 체크
3. **BOT PERMISSIONS**에서 `Send Messages`, `Embed Links` 체크
4. 하단에 생성된 URL 복사 → 브라우저에서 열어 봇을 원하는 서버에 초대

## 3. 채널 ID 확인

1. Discord 앱 → 사용자 설정 → **"고급" → "개발자 모드"** 켜기
2. 시세를 게시할 채널을 우클릭 → **"채널 ID 복사"**

## 4. 설치 및 실행

```bash
# 1) 의존성 설치
pip install -r requirements.txt

# 2) 환경변수 설정
cp .env.example .env
# .env 파일을 열어 DISCORD_TOKEN, CHANNEL_ID 입력

# 3) 봇 실행
python bot.py
```

## 5. 게시 시각 변경

`.env`의 `POST_TIME`을 수정하세요 (한국시간 기준).

```env
POST_TIME=07:00   # 매일 아침 7시 (미국장 마감 후 일일 요약)
POST_TIME=15:45   # 한국장 마감 후
```

## 6. 24시간 상시 실행

로컬 PC를 끄면 봇도 멈춥니다. 상시 실행하려면:
- **Windows**: 작업 스케줄러로 부팅 시 `python bot.py` 자동 실행
- **무료 호스팅**: Railway, Fly.io, Oracle Cloud Free Tier 등
- **VPS**: AWS Lightsail, Vultr 등에 올리고 `pm2`/`systemd`로 데몬화

## 출력 예시

```
📊 오늘의 시장 시세
기준: 2026-04-10 07:00 KST · 전일 종가 대비

미국 지수
S&P 500                         5,234.18  🔺 +18.42 (+0.35%)
나스닥                          16,442.20  🔺 +52.10 (+0.32%)
...
```

## 데이터 소스

[Yahoo Finance](https://finance.yahoo.com/) (`yfinance` 라이브러리). API 키 불필요, 무료. 단, 실시간이 아닌 약 15분 지연 데이터입니다.
