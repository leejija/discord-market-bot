"""
Discord 시장 시세 봇
- 미국/한국 대표 지수, 원유 선물, 미국/한국 단기·장기 국채 시세를 매일 자동 게시
"""
import os
import re
import asyncio
import logging
import urllib.request
from datetime import datetime
from typing import Optional

import discord
import yfinance as yf
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("market-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # 0이면 글로벌 sync (느림)
# 쉼표로 여러 시각 지정 가능 (예: "07:00,16:00")
POST_TIME = os.getenv("POST_TIME", "07:00,16:00")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
KST = pytz.timezone("Asia/Seoul")

# Claude 클라이언트 (API 키가 있을 때만 활성화)
_anthropic_client = (
    Anthropic(api_key=ANTHROPIC_API_KEY)
    if (Anthropic and ANTHROPIC_API_KEY)
    else None
)

# (카테고리, [(표시이름, 야후 티커), ...])
TICKER_GROUPS = [
    ("미국 지수", [
        ("S&P 500",  "^GSPC"),
        ("나스닥",   "^IXIC"),
        ("다우존스", "^DJI"),
        ("러셀2000", "^RUT"),
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
        ("달러/원", "KRW=X"),
        ("유로/원", "EURKRW=X"),
        ("엔화/원", "JPYKRW=X"),
        ("위안/원", "CNYKRW=X"),
    ]),
    # 미국 국채: 10년물 실제 금리(%) — ^TNX 는 4.35 처럼 수익률 자체를 반환
    ("미국 국채 금리", [
        ("미국 10년물 (%)", "^TNX"),
    ]),
    # 한국 금리: 네이버 금융 시장지표 스크래핑 (국고채 10년물은 네이버 미제공)
    ("한국 국채 금리", [
        ("국고채 3년 (%)", "naver:IRR_GOVT03Y"),
        ("회사채 3년 (%)", "naver:IRR_CORP03Y"),
    ]),
]

# 시장별 워치리스트 (top movers 계산용)
US_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA",
    "JPM", "V", "JNJ", "WMT", "PG", "MA", "HD", "BAC", "XOM",
    "AVGO", "LLY", "UNH", "COST", "ORCL", "NFLX", "AMD", "INTC",
    "DIS", "BA", "GS", "CAT", "MCD", "KO",
]
KR_WATCHLIST = [
    "005930.KS",  # 삼성전자
    "000660.KS",  # SK하이닉스
    "035420.KS",  # NAVER
    "035720.KS",  # 카카오
    "005380.KS",  # 현대차
    "000270.KS",  # 기아
    "005490.KS",  # POSCO홀딩스
    "051910.KS",  # LG화학
    "006400.KS",  # 삼성SDI
    "068270.KS",  # 셀트리온
    "207940.KS",  # 삼성바이오로직스
    "105560.KS",  # KB금융
    "055550.KS",  # 신한지주
    "066570.KS",  # LG전자
    "012330.KS",  # 현대모비스
    "028260.KS",  # 삼성물산
    "032830.KS",  # 삼성생명
    "017670.KS",  # SK텔레콤
    "015760.KS",  # 한국전력
    "003670.KS",  # 포스코퓨처엠
]

# 종목명 캐시 (yf.Ticker.info 호출이 느려서 모듈 단위로 캐시)
_TICKER_NAME_CACHE: dict[str, str] = {
    # 한국 종목은 yfinance가 영어 이름을 주는 경우가 많아 미리 한글로 매핑
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
    "005380.KS": "현대차",
    "000270.KS": "기아",
    "005490.KS": "POSCO홀딩스",
    "051910.KS": "LG화학",
    "006400.KS": "삼성SDI",
    "068270.KS": "셀트리온",
    "207940.KS": "삼성바이오로직스",
    "105560.KS": "KB금융",
    "055550.KS": "신한지주",
    "066570.KS": "LG전자",
    "012330.KS": "현대모비스",
    "028260.KS": "삼성물산",
    "032830.KS": "삼성생명",
    "017670.KS": "SK텔레콤",
    "015760.KS": "한국전력",
    "003670.KS": "포스코퓨처엠",
}

# 뉴스 소스 티커 (시장별 대표 ETF — yfinance 뉴스가 풍부)
NEWS_TICKER_US = "SPY"
NEWS_TICKER_KR = "EWY"


def fetch_naver_rate(market_index_cd: str) -> Optional[dict]:
    """네이버 금융 시장지표에서 국내 금리(%)를 가져온다.
    반환 형태는 fetch_quote와 동일: {"last", "change", "pct"}.
    last/change 단위는 %(퍼센트포인트)."""
    url = (
        "https://finance.naver.com/marketindex/interestDailyQuote.naver"
        f"?marketindexCd={market_index_cd}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("euc-kr", errors="replace")
    except Exception as e:
        log.warning("fetch_naver_rate(%s) 요청 실패: %s", market_index_cd, e)
        return None

    # 최신 행 하나만 파싱: <tr class="up|down|same2"> ... <td class="num">금리</td>
    #                     <td class="num"><img alt=...> 변동폭</td>
    m = re.search(
        r'<tr class="(up|down|same2?)">.*?'
        r'<td class="num">([\d.]+)</td>.*?'
        r'<td class="num">.*?([\d.]+)\s*</td>',
        html,
        re.S,
    )
    if not m:
        log.warning("fetch_naver_rate(%s) 파싱 실패", market_index_cd)
        return None

    direction, last_s, delta_s = m.group(1), m.group(2), m.group(3)
    try:
        last = float(last_s)
        delta = float(delta_s)
    except ValueError:
        return None

    change = -delta if direction == "down" else (delta if direction == "up" else 0.0)
    prev = last - change
    pct = (change / prev) * 100 if prev else 0.0
    return {"last": last, "change": change, "pct": pct}


def fetch_any(ticker: str) -> Optional[dict]:
    """티커 문자열에 따라 적절한 소스로 분기.
    'naver:CODE' 형식이면 네이버 금융, 그 외는 야후 파이낸스."""
    if ticker.startswith("naver:"):
        return fetch_naver_rate(ticker.split(":", 1)[1])
    return fetch_quote(ticker)


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


# Discord ANSI 색상 (```ansi 블록 안에서만 동작)
GREEN = "\u001b[32m"
RED   = "\u001b[31m"
RESET = "\u001b[0m"


def format_line(name: str, q: Optional[dict]) -> str:
    if q is None:
        return f"{name}  데이터 없음"
    arrow = "🟢▲" if q["change"] > 0 else ("🔴▼" if q["change"] < 0 else "⬜➖")
    sign  = "+" if q["change"] >= 0 else ""
    color = GREEN if q["change"] > 0 else (RED if q["change"] < 0 else "")
    reset = RESET if color else ""
    # 이름·가격·이모지는 흰색, 퍼센트(%)만 색상
    return (
        f"{name}  {q['last']:,.2f}  {arrow} "
        f"{sign}{q['change']:,.2f} {color}({sign}{q['pct']:.2f}%){reset}"
    )


def build_embed() -> discord.Embed:
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    embed = discord.Embed(
        title="📊 오늘의 시장 시세",
        description=f"기준: {now_kst} · 전일 종가 대비",
        color=0x2ecc71,
    )
    for category, items in TICKER_GROUPS:
        lines = [format_line(name, fetch_any(tk)) for name, tk in items]
        value = "```ansi\n" + "\n".join(lines) + "\n```"
        embed.add_field(name=f"**{category}**", value=value, inline=False)
    embed.set_footer(text="Source: Yahoo Finance (yfinance)")
    return embed


# ============================================================
#  증시 이슈 요약 (Top movers + 뉴스 헤드라인)
# ============================================================

def _resolve_name(ticker: str) -> str:
    """티커 → 표시 이름. 캐시 → yfinance.info → ticker 순으로 fallback."""
    if ticker in _TICKER_NAME_CACHE:
        return _TICKER_NAME_CACHE[ticker]
    try:
        info = yf.Ticker(ticker).info or {}
        name = info.get("shortName") or info.get("longName") or ticker
    except Exception:
        name = ticker
    _TICKER_NAME_CACHE[ticker] = name
    return name


def fetch_top_movers(tickers: list, n: int = 3) -> list:
    """워치리스트에서 |% 변동| 상위 n개 반환.
    각 항목: {"ticker", "name", "last", "change", "pct"}.
    실패한 티커는 건너뛴다."""
    results = []
    for tk in tickers:
        q = fetch_quote(tk)
        if q is None:
            continue
        results.append({
            "ticker": tk,
            "name": _resolve_name(tk),
            "last": q["last"],
            "change": q["change"],
            "pct": q["pct"],
        })
    results.sort(key=lambda r: abs(r["pct"]), reverse=True)
    return results[:n]


def _fetch_news_for_ticker(ticker: str, n: int = 3) -> list:
    """단일 티커의 yfinance 뉴스 n개. 각 항목: title/link/publisher/summary."""
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as e:
        log.warning("_fetch_news_for_ticker(%s) 실패: %s", ticker, e)
        return []
    out = []
    for item in raw[:n * 2]:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, dict):
            title = content.get("title") or ""
            summary = content.get("summary") or content.get("description") or ""
            link = (
                (content.get("clickThroughUrl") or {}).get("url")
                or (content.get("canonicalUrl") or {}).get("url")
                or ""
            )
            publisher = (content.get("provider") or {}).get("displayName") or ""
        else:
            title = item.get("title") or ""
            summary = item.get("summary") or ""
            link = item.get("link") or ""
            publisher = item.get("publisher") or ""
        if not title:
            continue
        if len(title) > 70:
            title = title[:67] + "…"
        summary = " ".join(summary.split())
        if len(summary) > 180:
            summary = summary[:177] + "…"
        out.append({"title": title, "link": link, "publisher": publisher, "summary": summary})
        if len(out) >= n:
            break
    return out


def fetch_related_news(movers: list, fallback_ticker: str, n: int = 5) -> list:
    """top movers 각 종목당 1개씩 뉴스를 모아 n개 반환. 부족하면 fallback에서 채움.
    각 항목: title/link/publisher/summary/related_ticker/related_name."""
    out = []
    used_titles = set()
    # 1) 각 mover 종목당 첫 뉴스 1개씩
    for m in movers:
        items = _fetch_news_for_ticker(m["ticker"], n=2)
        for item in items:
            if item["title"] in used_titles:
                continue
            item["related_ticker"] = m["ticker"]
            item["related_name"] = m["name"]
            out.append(item)
            used_titles.add(item["title"])
            break
        if len(out) >= n:
            break
    # 2) 부족하면 fallback(시장 인덱스 ETF)에서 채움
    if len(out) < n:
        items = _fetch_news_for_ticker(fallback_ticker, n=n * 2)
        for item in items:
            if item["title"] in used_titles:
                continue
            item["related_ticker"] = None
            item["related_name"] = None
            out.append(item)
            used_titles.add(item["title"])
            if len(out) >= n:
                break
    return out[:n]


def synthesize_market(movers: list, news_items: list, market: str, korean: bool) -> str:
    """종목 등락 Top + 관련 뉴스를 Claude로 종합 요약. API 키 없으면 빈 문자열."""
    if not _anthropic_client or (not movers and not news_items):
        return ""
    market_label = "한국" if market == "kr" else "미국"

    # 가격 변동 블록
    if movers:
        movers_block_lines = []
        for i, m in enumerate(movers, 1):
            sign = "+" if m["pct"] >= 0 else ""
            price_fmt = f"{m['last']:,.0f}" if korean else f"{m['last']:,.2f}"
            movers_block_lines.append(
                f"{i}. {m['name']} ({m['ticker']}): {price_fmt}, {sign}{m['pct']:.2f}%"
            )
        movers_block = "\n".join(movers_block_lines)
    else:
        movers_block = "(데이터 없음)"

    # 뉴스 블록
    if news_items:
        news_block_lines = []
        for i, n in enumerate(news_items, 1):
            related = f" [관련: {n.get('related_name')}]" if n.get("related_name") else ""
            news_block_lines.append(
                f"{i}. 제목: {n['title']}{related}\n"
                f"   출처: {n.get('publisher', '')}\n"
                f"   본문 발췌: {n.get('summary', '')}"
            )
        news_block = "\n\n".join(news_block_lines)
    else:
        news_block = "(데이터 없음)"

    prompt = (
        f"다음은 오늘 {market_label} 증시의 주요 종목 등락과 관련 뉴스입니다.\n\n"
        f"📊 주요 종목 등락 (Top {len(movers)}):\n{movers_block}\n\n"
        f"📰 관련 뉴스 ({len(news_items)}건):\n{news_block}\n\n"
        "위 가격 변동 데이터와 뉴스 내용을 종합하여, 오늘 시장의 핵심 흐름과 "
        "주요 종목의 움직임 배경을 한국어로 자연스러운 한 단락(3~4문장, 350자 이내)으로 "
        "정리해 주세요. 인사말이나 부연설명 없이 요약 단락만 응답하세요."
    )
    try:
        msg = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in msg.content:
            if getattr(block, "type", "") == "text":
                text += block.text
        return text.strip()
    except Exception as e:
        log.warning("synthesize_market 실패: %s", e)
        return ""


def _format_mover_line(m: dict, korean: bool) -> str:
    """top mover 한 줄 포맷. 한국 종목은 정수, 미국 종목은 소수 둘째 자리."""
    arrow = "🟢▲" if m["change"] > 0 else ("🔴▼" if m["change"] < 0 else "⬜➖")
    sign = "+" if m["pct"] >= 0 else ""
    color = GREEN if m["pct"] > 0 else (RED if m["pct"] < 0 else "")
    reset = RESET if color else ""
    price_fmt = f"{m['last']:,.0f}" if korean else f"{m['last']:,.2f}"
    return f"{m['name']:<12} {price_fmt:>10}  {arrow} {color}{sign}{m['pct']:.2f}%{reset}"


def build_summary_embed(market: str) -> discord.Embed:
    """market: 'kr' 또는 'us'. 종목 등락 Top 3 + 뉴스 헤드라인 3개를 임베드로."""
    is_kr = market == "kr"
    title = "📰 한국 증시 요약" if is_kr else "📰 미국 증시 요약"
    color = 0x3498db if is_kr else 0xe67e22
    watchlist = KR_WATCHLIST if is_kr else US_WATCHLIST
    news_ticker = NEWS_TICKER_KR if is_kr else NEWS_TICKER_US

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    embed = discord.Embed(
        title=title,
        description=f"기준: {now_kst} · 전일 종가 대비",
        color=color,
    )

    # 1) 주요 종목 등락 Top 5
    movers = fetch_top_movers(watchlist, n=5)
    if movers:
        lines = [_format_mover_line(m, korean=is_kr) for m in movers]
        movers_value = "```ansi\n" + "\n".join(lines) + "\n```"
    else:
        movers_value = "데이터 없음"
    embed.add_field(name="🔥 주요 종목 등락 (Top 5)", value=movers_value, inline=False)

    # 2) Top 5 종목과 연관된 뉴스 5개 (각 종목당 1개씩, 부족하면 인덱스 ETF로 채움)
    news = fetch_related_news(movers, fallback_ticker=news_ticker, n=5)

    # 3) AI 종합 요약 (등락 + 뉴스를 모두 종합)
    if movers or news:
        synthesis = synthesize_market(movers, news, market, korean=is_kr)
        if synthesis:
            if len(synthesis) > 1020:
                synthesis = synthesis[:1017] + "…"
            embed.add_field(name="📰 종합 요약", value=synthesis, inline=False)

    # 4) 뉴스 링크 5개
    if news:
        bullets = []
        for i, n in enumerate(news, 1):
            line = f"{i}. [{n['title']}]({n['link']})" if n["link"] else f"{i}. {n['title']}"
            extras = []
            if n.get("publisher"):
                extras.append(f"*{n['publisher']}*")
            if n.get("related_name"):
                extras.append(f"`{n['related_name']}`")
            if extras:
                line += " — " + " · ".join(extras)
            bullets.append(line)
        links_value = "\n".join(bullets)
        if len(links_value) > 1020:
            links_value = links_value[:1017] + "…"
        embed.add_field(name="🔗 뉴스 링크", value=links_value, inline=False)
    else:
        embed.add_field(name="📌 주요 뉴스", value="뉴스 데이터 없음", inline=False)

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


async def build_summary_embed_async(market: str) -> discord.Embed:
    """이슈 요약 임베드를 별도 스레드에서 생성 (yfinance 동기 호출 차단 방지)."""
    return await asyncio.to_thread(build_summary_embed, market)


async def post_summary(market: str):
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("채널 ID %s 를 찾을 수 없습니다.", CHANNEL_ID)
        return
    label = "한국" if market == "kr" else "미국"
    log.info("%s 증시 요약 생성 중…", label)
    embed = await build_summary_embed_async(market)
    await channel.send(embed=embed)
    log.info("%s 증시 요약 게시 완료", label)


async def post_summary_kr():
    await post_summary("kr")


async def post_summary_us():
    await post_summary("us")


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


@tree.command(name="이슈한국", description="한국 증시 이슈 요약 (Top 종목 + 뉴스)")
async def slash_issue_kr(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        embed = await build_summary_embed_async("kr")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("/이슈한국 처리 중 오류")
        await interaction.followup.send(f"⚠️ 한국 증시 요약 실패: {e}")


@tree.command(name="이슈미국", description="미국 증시 이슈 요약 (Top 종목 + 뉴스)")
async def slash_issue_us(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        embed = await build_summary_embed_async("us")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("/이슈미국 처리 중 오류")
        await interaction.followup.send(f"⚠️ 미국 증시 요약 실패: {e}")


@client.event
async def on_ready():
    log.info("로그인: %s", client.user)
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            # 1) 길드 스코프로 명령어 동기화 (즉시 반영)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            log.info("길드(%s) 슬래시 명령어 동기화 완료: %d개", GUILD_ID, len(synced))
            # 2) 글로벌 스코프에 남아있는 중복 명령어 제거 (자동완성에 두 번 뜨는 문제 방지)
            tree.clear_commands(guild=None)
            await tree.sync()
            log.info("글로벌 스코프 슬래시 명령어 정리 완료 (중복 제거)")
        else:
            synced = await tree.sync()
            log.info("글로벌 슬래시 명령어 동기화 완료: %d개 (반영까지 최대 1시간)", len(synced))
    except Exception as e:
        log.error("슬래시 명령어 동기화 실패: %s", e)

    # 일일 시세 — POST_TIME에 쉼표로 여러 시각을 지정할 수 있음 (기본 07:00, 16:00)
    for idx, slot in enumerate(POST_TIME.split(",")):
        slot = slot.strip()
        if not slot:
            continue
        try:
            h, m = map(int, slot.split(":"))
        except ValueError:
            log.error("POST_TIME 형식 오류로 건너뜀: %r (HH:MM 형식이어야 함)", slot)
            continue
        scheduler.add_job(
            post_market_update,
            CronTrigger(hour=h, minute=m, timezone=KST),
            id=f"daily_market_{idx}",
            replace_existing=True,
        )
        log.info("스케줄 등록 완료(시세): 매일 %02d:%02d KST", h, m)

    if not scheduler.running:
        scheduler.start()


if __name__ == "__main__":
    if not TOKEN or not CHANNEL_ID:
        raise SystemExit("DISCORD_TOKEN 과 CHANNEL_ID 를 .env 에 설정하세요.")
    client.run(TOKEN)
