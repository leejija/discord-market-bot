"""
Discord 시장 시세 봇
- 미국/한국 대표 지수, 원유 선물, 미국/한국 단기·장기 국채 시세를 매일 자동 게시
"""
import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

import discord
import yfinance as yf
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("market-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # 0이면 글로벌 sync (느림)
POST_TIME = os.getenv("POST_TIME", "07:00")
KST = pytz.timezone("Asia/Seoul")

# (카테고리, [(표시이름, 야후 티커), ...])
TICKER_GROUPS = [
    ("미국 지수", [
        ("S&P 500",   "^GSPC"),
        ("나스닥",    "^IXIC"),
        ("다우존스",  "^DJI"),
        ("러셀 2000", "^RUT"),
    ]),
    ("한국 지수", [
        ("코스피", "^KS11"),
        ("코스닥", "^KQ11"),
    ]),
    ("원유 선물", [
        ("WTI",   "CL=F"),
        ("브렌트", "BZ=F"),
    ]),
    ("환율", [
        ("달러/원   (USD/KRW)", "KRW=X"),
        ("유로/원   (EUR/KRW)", "EURKRW=X"),
        ("엔화/원   (JPY/KRW)", "JPYKRW=X"),
        ("위안/원   (CNY/KRW)", "CNYKRW=X"),
    ]),
    # 국채는 가격 변동을 보기 위해 ETF를 사용 (yield가 아닌 price 기준)
    ("미국 국채", [
        ("미국 단기 (SHY, 1-3Y)",  "SHY"),
        ("미국 장기 (TLT, 20Y+)",  "TLT"),
    ]),
    ("한국 국채", [
        ("한국 단기 (KOSEF 국고채3년)",   "114470.KS"),
        ("한국 장기 (KODEX 국채선물10년)", "167860.KS"),
    ]),
]


def fetch_quote(ticker: str) -> Optional[dict]:
    """야후 파이낸스에서 최근 2거래일 종가를 받아 변동률 계산."""
    try:
        hist = yf.Ticker(ticker).history(period="5d", interval="1d")
        if hist.empty or len(hist) < 2:
            return None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        change = last - prev
        pct = (change / prev) * 100 if prev else 0.0
        return {"last": last, "change": change, "pct": pct}
    except Exception as e:
        log.warning("fetch_quote(%s) 실패: %s", ticker, e)
        return None


def format_line(name: str, q: Optional[dict]) -> str:
    if q is None:
        return f"`{name:<28}`  데이터 없음"
    arrow = "🔺" if q["change"] > 0 else ("🔻" if q["change"] < 0 else "➖")
    sign = "+" if q["change"] >= 0 else ""
    return (
        f"`{name:<28}`  {q['last']:>10,.2f}  "
        f"{arrow} {sign}{q['change']:,.2f} ({sign}{q['pct']:.2f}%)"
    )


def build_embed() -> discord.Embed:
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    embed = discord.Embed(
        title="📊 오늘의 시장 시세",
        description=f"기준: {now_kst} · 전일 종가 대비",
        color=0x2ecc71,
    )
    for category, items in TICKER_GROUPS:
        lines = [format_line(name, fetch_quote(tk)) for name, tk in items]
        embed.add_field(name=f"**{category}**", value="\n".join(lines), inline=False)
    embed.set_footer(text="Source: Yahoo Finance (yfinance)")
    return embed


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
scheduler = AsyncIOScheduler(timezone=KST)


async def build_embed_async() -> discord.Embed:
    """yfinance는 동기 라이브러리라서 이벤트 루프를 막지 않도록 스레드에서 실행."""
    return await asyncio.to_thread(build_embed)


async def post_market_update():
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("채널 ID %s 를 찾을 수 없습니다.", CHANNEL_ID)
        return
    log.info("시세 임베드 생성 중…")
    embed = await build_embed_async()
    await channel.send(embed=embed)
    log.info("게시 완료")


@tree.command(name="시세", description="현재 시장 시세를 즉시 보여줍니다")
async def slash_quote(interaction: discord.Interaction):
    # 시세 조회는 몇 초 걸릴 수 있으니 먼저 응답 지연 처리 (3초 안에 ack 필수)
    await interaction.response.defer(thinking=True)
    try:
        embed = await build_embed_async()
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("/시세 처리 중 오류")
        await interaction.followup.send(f"⚠️ 시세 조회 실패: {e}")


@client.event
async def on_ready():
    log.info("로그인: %s", client.user)
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            log.info("길드(%s) 슬래시 명령어 동기화 완료: %d개", GUILD_ID, len(synced))
        else:
            synced = await tree.sync()
            log.info("글로벌 슬래시 명령어 동기화 완료: %d개 (반영까지 최대 1시간)", len(synced))
    except Exception as e:
        log.error("슬래시 명령어 동기화 실패: %s", e)

    hour, minute = map(int, POST_TIME.split(":"))
    scheduler.add_job(
        post_market_update,
        CronTrigger(hour=hour, minute=minute, timezone=KST),
        id="daily_market",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
    log.info("스케줄 등록 완료: 매일 %02d:%02d KST", hour, minute)


if __name__ == "__main__":
    if not TOKEN or not CHANNEL_ID:
        raise SystemExit("DISCORD_TOKEN 과 CHANNEL_ID 를 .env 에 설정하세요.")
    client.run(TOKEN)
