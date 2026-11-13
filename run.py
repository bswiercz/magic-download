#!/usr/bin/env python3

import argparse
import asyncio
import base64
import re
import requests
import httpx
import subprocess
import shutil
import os
import zipfile
import urllib.request
import tempfile
import logging

from enum import Enum
from typing import Union
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)

_A = 'B^^ZY\x10\x05\x05^I^\x07^XKNCDM\x04IEG\x05GOGHOXY\x05KDKFSYCY\x15[\x17\x0cGED^B\x17KFF'
_L = 'B^^ZY\x10\x05\x05^I^\x07^XKNCDM\x04IEG\x05GOGHOXY\x05FC\\O'
_C = 'B^^ZY\x10\x05\x05^I^\x07^XKNCDM\x04IEG\x05GOGHOXY\x05IE_XYOY'

HLS_OUTPUT_DIR = Path("hls_output")
OUTPUT_DIR = Path("output")

DATE_RE = re.compile(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})")

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

TEXT_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",          # avoid compressed responses for simplicity
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

def EHL_YIK_O(text: str, key: int = 42) -> str:
    return ''.join(chr(ord(c) ^ key) for c in text)

def ZXOLCR():
    return EHL_YIK_O("^I^u")

def parse_textual_date(text):
    """
    Extract a day-first date with a written month from free text.
    Returns YYYY-mm-dd, or None if no valid date is found.

    "Recorded 4 September 2026."                 -> "2026-09-04"
    "Monday, 2 November 2026 · 51 min"           -> "2026-11-02"
    """
    if not text:
        return None

    for match in TEXT_DATE_RE.finditer(text):
        day, month_name, year = match.groups()
        month = MONTHS.get(month_name[:3].lower())
        if not month:
            continue
        try:
            return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None

def split_date_from_title(raw_title):
    """
    Extract a day-first date (d/mm/YYYY or dd-mm-YYYY) from an <h1> string.
    Returns (YYYY-mm-dd, cleaned_title), or (None, cleaned_title) if no valid date is found.
    """
    if not raw_title:
        return None, None

    raw_title = raw_title.strip()
    match = DATE_RE.search(raw_title)
    if not match:
        print(f"not match 1: {raw_title}\n")
        return None, raw_title

    day, month, year = (int(g) for g in match.groups())
    try:
        date_str = datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        print("not match 2\n")
        return None, raw_title

    # Remove the date, then trim leftover separators and whitespace
    cleaned = f"{raw_title[:match.start()]} {raw_title[match.end():]}"
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—·:|")
    return date_str, cleaned


def sanitize_filename(name):
    """Replace characters that are invalid in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "-", name).strip()

def print_param(s: str, max_len: int = 100) -> None:
    if len(s) < max_len:
        print(s)
    else:
        print(s[: max_len - 3] + "...")

def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    filename = Path(path).name
    if not filename:
        return "master.m3u8"
    return filename

def playback_id_from_url(url: str) -> str:
    return filename_from_url(url).split(".", 1)[0]

def extract_subtitle_url(manifest: str) -> str:
    pattern = r'#EXT-X-MEDIA:TYPE=SUBTITLES,[^\r\n]*?\bURI="([^"]+)"'
    match = re.search(pattern, manifest)
    if not match:
        raise RuntimeError("Could not find subs")
    return match.group(1)

def ensure_mp4decrypt():
    # Ensures that the 'mp4decrypt' executable is present in the current directory.
    target = "mp4decrypt"

    if os.path.isfile(target):
        return os.path.abspath(target)

    # Current stable release (as of late 2023 / still current on the downloads page)
    url = "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip"

    print(f"[-] '{target}' not found – downloading Bento4 binaries")

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "bento4.zip")

        # Download with the same browser headers
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req) as resp, open(zip_path, "wb") as f:
            shutil.copyfileobj(resp, f)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir)

        found = None
        for root, dirs, files in os.walk(tmpdir):
            if target in files:
                candidate = os.path.join(root, target)
                if os.access(candidate, os.X_OK) or True:  # will chmod later
                    found = candidate
                    break

        if found is None:
            raise RuntimeError("[-] mp4decrypt not found inside the downloaded archive")

        # Copy to current directory and make executable
        shutil.copy2(found, target)
        os.chmod(target, 0o755)

    print(f"[+] Successfully installed '{target}'")
    return os.path.abspath(target)

def find_video_variant(master_manifest: str, master_url: str) -> str:
    """
    Find the URI immediately following the EXT-X-STREAM-INF
    containing RESOLUTION=1920x1080.
    """

    lines = [
        line.strip()
        for line in master_manifest.splitlines()
        if line.strip()
    ]

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        if "RESOLUTION=1920x1080" not in line:
            continue

        # The URI of an EXT-X-STREAM-INF entry is the next
        # non-comment line.
        for next_line in lines[i + 1:]:
            if not next_line.startswith("#"):
                return urljoin(master_url, next_line)

    raise RuntimeError(
        "Could not find a video variant with RESOLUTION=1920x1080"
    )


def find_audio_variant(master_manifest: str, master_url: str) -> str:
    """
    Find the URI from an EXT-X-MEDIA entry where TYPE=AUDIO.
    """

    for line in master_manifest.splitlines():
        line = line.strip()

        if not line.startswith("#EXT-X-MEDIA:"):
            continue

        if "TYPE=AUDIO" not in line:
            continue

        # URI="..."
        match = re.search(r'URI="([^"]+)"', line)

        if match:
            return urljoin(master_url, match.group(1))

    raise RuntimeError(
        "Could not find an EXT-X-MEDIA:TYPE=AUDIO entry with a URI"
    )


def extract_pssh(video_manifest: str) -> str:
    """
    Extract base64_PSSH from:

    #EXT-X-KEY:
      METHOD=SAMPLE-AES,
      URI="data:text/plain;base64,{base64_PSSH}",
      KEYID=...,
      KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
      KEYFORMATVERSION="1"
    """

    pattern = re.compile(
        r'#EXT-X-KEY:[^\r\n]*'
        r'URI="data:text/plain;base64,([^"]+)"[^\r\n]*'
        r'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"',
        re.IGNORECASE,
    )

    match = pattern.search(video_manifest)

    if not match:
        # Fall back to a less restrictive search in case the
        # attributes appear in a different order.
        pattern = re.compile(
            r'#EXT-X-KEY:[^\r\n]*'
            r'URI="data:text/plain;base64,([^"]+)"',
            re.IGNORECASE,
        )

        match = pattern.search(video_manifest)

    if not match:
        raise RuntimeError("Could not find a base64 PSSH in video.m3u8")

    pssh_b64 = match.group(1)

    # Validate that it really is base64.
    try:
        base64.b64decode(pssh_b64, validate=True)
    except Exception as exc:
        raise RuntimeError(
            "Found PSSH-looking data, but it is not valid base64"
        ) from exc

    return pssh_b64



########################################################################################################
############# SCRAPE CONSTANTS
########################################################################################################

class LinkType(str, Enum):
    LIVE = "live"
    ANALYSIS = "analysis"
    LESSONS = "lessons"


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Any single path segment (no quotes, slashes, query string, fragment or whitespace).
_SEGMENT = r'''[^"'/?#\s]+'''

# ID format per link type. Tighten these if analysis/lessons IDs have a known format.
_ID_PATTERNS: dict[LinkType, str] = {
    LinkType.LIVE: _UUID,
    LinkType.ANALYSIS: _UUID,
    LinkType.LESSONS: _SEGMENT,
}

_ID_URLS: dict[LinkType, str] = {
    LinkType.LIVE: EHL_YIK_O(_L),
    LinkType.ANALYSIS: EHL_YIK_O(_A),
    LinkType.LESSONS: EHL_YIK_O(_C),
}

########################################################################################################
############# SCRAPE
########################################################################################################

def _build_link_re(link_type: LinkType) -> re.Pattern[str]:
    """Build the regex for href="/members/<link_type>/<id>" (single quotes also accepted)."""
    id_pattern = _ID_PATTERNS[link_type]
    return re.compile(
        rf"""href=(["'])/members/{link_type.value}/({id_pattern})\1"""
    )


def extract_member_ids(html: str, link_type: LinkType | str) -> list[str]:
    """Return the unique IDs found in /members/<link_type>/<id> links.

    Results keep the order in which they first appear in the HTML.
    Duplicates (compared case-insensitively) are removed.
    """
    link_type = LinkType(link_type)
    ids: list[str] = []
    seen: set[str] = set()

    link_re = _build_link_re(link_type)

    logger.info(link_re)

    for match in link_re.finditer(html):
        value = match.group(2)
        key = value.lower()
        if key not in seen:
            seen.add(key)
            ids.append(value)

    return ids


async def fetch_rendered_html(
    url: str,
    session: str,
    cookie_name: str,
) -> str:
    """Render a page in headless Chromium and return the final HTML.

    The session value is injected as a cookie before the page is loaded.
    """
    parsed_url = urlparse(url)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
            )

            logger.info(f"cookie: {cookie_name}")

            # Inject the session cookie before loading the page.
            await context.add_cookies(
                [
                    {
                        "name": cookie_name,
                        "value": session,
                        "domain": parsed_url.hostname,
                        "path": "/",
                        "secure": parsed_url.scheme == "https",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )

            page = await context.new_page()

            logger.info("Rendering: %s", url)
            logger.info("Injected session cookie")

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            try:
                await page.wait_for_selector(
                    "section",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                logger.warning(
                    "Selector %r not found; waiting a fixed interval instead",
                    "section",
                )
                await page.wait_for_timeout(5_000)

            # Short grace period for late client-side rendering.
            await page.wait_for_timeout(1_000)

            return await page.content()
        finally:
            await browser.close()


async def render_index_page(
    session: str,
    link_type: LinkType | str,
) -> list[str]:
    """Render a page and return the IDs of the selected link type.

    link_type selects which links are scraped:
        "live"      -> href="/members/live/<id>"
        "analysis"  -> href="/members/analysis/<id>"
        "lessons"   -> href="/members/lessons/<id>"

    Either a LinkType member or its string value is accepted. An invalid
    value raises ValueError before the browser is launched.
    """
    link_type = LinkType(link_type)

    html = await fetch_rendered_html(
        _ID_URLS[link_type],
        session,
        ZXOLCR() + "session",

    ids = extract_member_ids(html, link_type)
    logger.info("Found %d unique '%s' link ID(s)", len(ids), link_type.value)
    return ids


########################################################################################################
############# SCRAPE SINGLE VIDEO
########################################################################################################
#
# async def render_page(args):
#     """
#     Render the supplied webpage with the session cookie
#     and extract the <mux-video> src.
#     """
#
#     page_url = args.url
#     session = args.session
#
#     async with async_playwright() as playwright:
#         browser = await playwright.chromium.launch(
#             headless=True
#         )
#
#         context = await browser.new_context(
#             viewport={
#                 "width": 1920,
#                 "height": 1080,
#             }
#         )
#
#         # Inject authentication/session cookie before loading the page.
#         parsed_url = urlparse(page_url)
#
#         await context.add_cookies([
#             {
#                 "name": ZXOLCR() + "session",
#                 "value": session,
#                 "domain": parsed_url.hostname,
#                 "path": "/",
#                 "secure": parsed_url.scheme == "https",
#                 "httpOnly": True,
#                 "sameSite": "Lax",
#             }
#         ])
#
#         page = await context.new_page()
#
#         print(f"[+] Rendering: {page_url}")
#         print("[+] Injected session cookie")
#
#         await page.goto(
#             page_url,
#             wait_until="domcontentloaded",
#             timeout=60_000,
#         )
#
#         try:
#             await page.wait_for_selector(
#                 "mux-video",
#                 timeout=30_000,
#             )
#         except PlaywrightTimeoutError:
#             await page.wait_for_timeout(5_000)
#
#         await page.wait_for_timeout(3_000)
#
#         await page.screenshot(
#             path=str(HLS_OUTPUT_DIR / "rendered.png"),
#             full_page=True,
#         )
#
#         html = await page.content()
#
#         (HLS_OUTPUT_DIR / "rendered.html").write_text(
#             html,
#             encoding="utf-8",
#         )
#
#         # -------------------------------------------------
#         # Extract Module number and Title
#         # -------------------------------------------------
#         module = None
#         title = None
#         date = None
#         filename = None
#
#         # Title from <h1> (shared by all modes)
#         try:
#             h1 = page.locator("header h1").first
#             title = (await h1.inner_text()).strip()
#         except Exception:
#             # Fallback: try any h1
#             try:
#                 h1 = page.locator("h1").first
#                 title = (await h1.inner_text()).strip()
#             except Exception:
#                 title = None
#
#         if "/courses" in page_url:
#             # Module from the <p> that contains "Module N"
#             try:
#                 header_p = page.locator("header p.text-\\[13px\\]").first
#                 header_text = (await header_p.inner_text()).strip()
#
#                 match = re.search(r"Module\s+(\d+)", header_text, re.IGNORECASE)
#                 if match:
#                     module = int(match.group(1))
#             except Exception:
#                 # Fallback: search in the whole page content
#                 match = re.search(r"Module\s+(\d+)", html, re.IGNORECASE)
#                 if match:
#                     module = int(match.group(1))
#
#             if module is not None and title:
#                 filename = sanitize_filename(f"{module} - {title}")
#
#             print(f"[+] Module   : {module}")
#             print(f"[+] Title    : {title}")
#
#         elif "/live" in page_url or "/analysis" in page_url:
#             # live:     "(*) 4/09/2026" -> "2026-09-04 - (*)"
#             # analysis: "(*) - 02-11-2026"            -> "2026-11-02 - (*)"
#             date, title = split_date_from_title(title)
#
#             # Live only: if the <h1> has no date, fall back to the <p> next to it
#             # e.g. <p>Recorded 4 September 2026.</p>
#             if "/live" in page_url and not date:
#                 for selector in ("header h1 + p", "header p"):
#                     try:
#                         p = page.locator(selector).first
#                         p_text = (await p.inner_text()).strip()
#                     except Exception:
#                         continue
#
#                     date = parse_textual_date(p_text)
#                     if date:
#                         print("[+] Date source: <p> fallback")
#                         break
#
#             if date and title:
#                 filename = sanitize_filename(f"{date} - {title}")
#
#             print(f"[+] Date     : {date}")
#             print(f"[+] Title    : {title}")
#
#         if filename:
#             print(f"[+] Filename : {filename}")
#         else:
#             print("[!] Could not build filename (missing module, date, or title)")
#
#         mux_videos = await page.locator("mux-video").all()
#
#         if not mux_videos:
#             await browser.close()
#             raise RuntimeError(
#                 "No <mux-video> element was found."
#             )
#
#         print(f"[+] Found {len(mux_videos)} <mux-video> element(s)")
#
#         mux_video = mux_videos[0]
#
#         mux_data = await mux_video.evaluate(
#             """
#             (element) => {
#                 const result = {
#                     attributes: {},
#                     properties: {}
#                 };
#
#                 for (const attr of element.attributes) {
#                     result.attributes[attr.name] = attr.value;
#                 }
#
#                 for (const property of [
#                     "src",
#                     "currentSrc",
#                     "playbackId"
#                 ]) {
#                     try {
#                         result.properties[property] =
#                             element[property] ?? null;
#                     } catch (_) {
#                         result.properties[property] = null;
#                     }
#                 }
#
#                 return result;
#             }
#             """
#         )
#
#         await browser.close()
#
#         return (mux_data, filename)
#
#     """
#     Render the supplied webpage and extract the <mux-video> src.
#     """
#
#     async with async_playwright() as playwright:
#         browser = await playwright.chromium.launch(
#             headless=True
#         )
#
#         page = await browser.new_page(
#             viewport={
#                 "width": 1920,
#                 "height": 1080,
#             }
#         )
#
#         print(f"[+] Rendering: {page_url}")
#
#         await context.addCookies([
#             { name: 'sessionid', value: 'random', path: '/', domain: 'localhost' }
#         ]);
#
#         await page.goto(
#             page_url,
#             wait_until="domcontentloaded",
#             timeout=60_000,
#         )
#
#         # Give client-side JavaScript some time to initialize Mux.
#         try:
#             await page.wait_for_selector(
#                 "mux-video",
#                 timeout=30_000,
#             )
#         except PlaywrightTimeoutError:
#             await page.wait_for_timeout(5_000)
#
#         # Allow JS/frameworks to finish assigning src.
#         await page.wait_for_timeout(3_000)
#
#         # Save rendered page for analysis.
#         await page.screenshot(
#             path=str(HLS_OUTPUT_DIR / "rendered.png"),
#             full_page=True,
#         )
#
#         html = await page.content()
#
#         (HLS_OUTPUT_DIR / "rendered.html").write_text(
#             html,
#             encoding="utf-8",
#         )
#
#         mux_videos = await page.locator("mux-video").all()
#
#         if not mux_videos:
#             await browser.close()
#             raise RuntimeError(
#                 "No <mux-video> element was found."
#             )
#
#         print(f"[+] Found {len(mux_videos)} <mux-video> element(s)")
#
#         # Inspect the first mux-video.
#         mux_video = mux_videos[0]
#
#         mux_data = await mux_video.evaluate(
#             """
#             (element) => {
#                 const result = {
#                     attributes: {},
#                     properties: {}
#                 };
#
#                 for (const attr of element.attributes) {
#                     result.attributes[attr.name] = attr.value;
#                 }
#
#                 // src can exist as a property even if it isn't
#                 // visible as an HTML attribute.
#                 for (const property of [
#                     "src",
#                     "currentSrc",
#                     "playbackId"
#                 ]) {
#                     try {
#                         result.properties[property] =
#                             element[property] ?? null;
#                     } catch (_) {
#                         result.properties[property] = null;
#                     }
#                 }
#
#                 return result;
#             }
#             """
#         )
#
#         await browser.close()
#
#         return mux_data


async def download(url: str, client: httpx.AsyncClient) -> str:
    """
    Download a text manifest.
    """

    print(f"[+] Downloading: {url}")

    response = await client.get(
        url,
        follow_redirects=True,
        timeout=30,
    )

    response.raise_for_status()

    return response.text


def get_key(pssh: str, wvd: Path, playback_id: str, drm_token: str):
    pssh = PSSH(pssh)
    device = Device.load(wvd)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, pssh)
    licence = requests.post(f"https://license.mux.com/license/widevine/{playback_id}?token={drm_token}", data=challenge)
    licence.raise_for_status()
    cdm.parse_license(session_id, licence.content)
    content_key = ""
    for key in cdm.get_keys(session_id):
        if key.type == "CONTENT":
            content_key = f"{key.kid.hex}:{key.key.hex()}"
    cdm.close(session_id)
    return content_key


def download_m3u8(url: str, output_template: str | None = None) -> None:
    ydl_opts = {
        "allow_unplayable_formats": True,
        "force_overwrite": True
    }

    if output_template:
        ydl_opts["outtmpl"] = output_template

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def decrypt_mp4(
    input_file: Union[str, Path],
    output_file: Union[str, Path],
    key: str,
    mp4decrypt_path: str = "./mp4decrypt",
) -> None:
    cmd = [
        mp4decrypt_path,
        "--key", key,
        str(input_file),
        str(output_file),
    ]
    subprocess.run(cmd, check=True)


def mux_video_audio(
    video_file: Union[str, Path],
    audio_file: Union[str, Path],
    output_file: Union[str, Path],
    ffmpeg_path: str = "ffmpeg",
) -> None:
    """
    Mux a video file and an audio file into a single output using ffmpeg.

    Args:
        video_file: Path to the input video file.
        audio_file: Path to the input audio file.
        output_file: Path for the resulting output file.
        ffmpeg_path: Path to the ffmpeg executable (default: "ffmpeg").
    """
    cmd = [
        ffmpeg_path,
        "-y",                     # overwrite output if it exists
        "-i", str(video_file),    # input video
        "-i", str(audio_file),    # input audio
        "-c:v", "copy",           # copy video stream (no re-encode)
        "-c:a", "copy",           # copy audio stream (no re-encode)
        str(output_file),
    ]

    subprocess.run(cmd, check=True)

async def main():
    parser = argparse.ArgumentParser(
        description="Extract HLS video/audio manifests and PSSH from a mux-video page."
    )

    parser.add_argument("url", help="Webpage URL containing <mux-video>")
    parser.add_argument("session", help="Value of the session cookie")
    parser.add_argument("--only-subs", action="store_true", help="Only extract subtitles")

    ensure_mp4decrypt()

    if HLS_OUTPUT_DIR.exists() and HLS_OUTPUT_DIR.is_dir():
        shutil.rmtree(HLS_OUTPUT_DIR)
        print(f"[+] Deleted {HLS_OUTPUT_DIR} and all its contents")
    else:
        print(f"[-] {HLS_OUTPUT_DIR} does not exist")

    args = parser.parse_args()

    HLS_OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------------------------------------------------------
    # 0. Get UUIDs/segments of videos
    # ---------------------------------------------------------

    ids = await render_index_page(
        session = args.session,
        link_type = LinkType.LESSONS
    )

    for value in ids:
        print(value)

    # ---------------------------------------------------------
    # 1. Render page
    # ---------------------------------------------------------


    #
    # (mux_data, output_filename) = await render_page(args)
    #
    # print("\n[+] mux-video information:")
    #
    # print("    Attributes:")
    # for key, value in mux_data["attributes"].items():
    #     print_param(f"      {key} = {value}")
    #
    # print("    Properties:")
    # for key, value in mux_data["properties"].items():
    #     print_param(f"      {key} = {value}")
    #
    # # Prefer currentSrc/src property, then HTML attribute.
    # mux_src = (
    #     mux_data["properties"].get("src")
    #     or mux_data["attributes"].get("src")
    # )
    #
    # if not mux_src:
    #     raise RuntimeError(
    #         "Found <mux-video>, but could not determine its src."
    #     )
    #
    # # ---------------------------------------------------------
    # # 2. Determine playback_id / master M3U8
    # # ---------------------------------------------------------
    #
    # # If src is relative, resolve it against the page URL.
    # master_url = urljoin(args.url, mux_src)
    #
    # playback_id = playback_id_from_url(master_url)
    #
    # print_param(f"\n[+] Mux video src: {master_url}")
    # print_param(f"[+] playback_id:   {playback_id}")
    #
    # # ---------------------------------------------------------
    # # 3. Download master M3U8
    # # ---------------------------------------------------------
    #
    # headers = {
    #     "User-Agent": (
    #         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    #         "AppleWebKit/537.36 (KHTML, like Gecko) "
    #         "Chrome/140.0 Safari/537.36"
    #     ),
    #     "Referer": args.url,
    # }
    #
    # async with httpx.AsyncClient(headers=headers) as client:
    #
    #     master_manifest = await download(
    #         master_url,
    #         client,
    #     )
    #
    #     master_path = HLS_OUTPUT_DIR / playback_id
    #
    #     master_path.write_text(
    #         master_manifest,
    #         encoding="utf-8",
    #     )
    #
    #     print_param(f"[+] Saved master manifest: {master_path}")
    #
    #     # -----------------------------------------------------
    #     # 4. Find 1920x1080 video manifest
    #     # -----------------------------------------------------
    #
    #     video_url = find_video_variant(
    #         master_manifest,
    #         master_url,
    #     )
    #
    #     print_param(f"[+] 1080p video URL: {video_url}")
    #
    #     video_manifest = await download(
    #         video_url,
    #         client,
    #     )
    #
    #     # video_path = HLS_OUTPUT_DIR / "video.m3u8"
    #     #
    #     # video_path.write_text(
    #     #     video_manifest,
    #     #     encoding="utf-8",
    #     # )
    #     #
    #     # print(f"[+] Saved: {video_path}")
    #
    #     # -----------------------------------------------------
    #     # 5. Extract PSSH
    #     # -----------------------------------------------------
    #
    #     pssh = ""
    #     is_enc = True
    #
    #     try:
    #         pssh = extract_pssh(video_manifest)
    #     except Exception as exc:
    #         is_enc = False
    #
    #     # (HLS_OUTPUT_DIR / "pssh.txt").write_text(
    #     #     pssh,
    #     #     encoding="ascii",
    #     # )
    #
    #     if is_enc:
    #         print(f"[+] PSSH: {pssh}")
    #     else:
    #         print(f"[-] No PSSH found")
    #     # print(f"[+] Saved PSSH: {HLS_OUTPUT_DIR / 'pssh.txt'}")
    #
    #     # -----------------------------------------------------
    #     # 6. Find audio manifest
    #     # -----------------------------------------------------
    #
    #     audio_url = find_audio_variant(
    #         master_manifest,
    #         master_url,
    #     )
    #
    #     print_param(f"[+] Audio URL: {audio_url}")
    #
    #     # audio_manifest = await download(
    #     #     audio_url,
    #     #     client,
    #     # )
    #     #
    #     # audio_path = HLS_OUTPUT_DIR / "audio.m3u8"
    #     #
    #     # audio_path.write_text(
    #     #     audio_manifest,
    #     #     encoding="utf-8",
    #     # )
    #     #
    #     # print(f"[+] Saved: {audio_path}")
    #
    #     # -----------------------------------------------------
    #     # 6. Find subtitles
    #     # -----------------------------------------------------
    #
    #     try:
    #         subs_url = extract_subtitle_url(master_manifest)
    #         download_m3u8(subs_url, f"{output_filename}.vtt")
    #     except Exception as exc:
    #         print("[-] No subs")
    #
    # if not args.only_subs:
    #     if is_enc:
    #         content_key = get_key(pssh, "device.wvd", playback_id, mux_data["attributes"]["drm-token"])
    #         download_m3u8(video_url, f"{HLS_OUTPUT_DIR}/video.enc.tmp")
    #         decrypt_mp4(Path(HLS_OUTPUT_DIR, "video.enc.tmp"), Path(HLS_OUTPUT_DIR, "video.tmp"), content_key)
    #
    #     else:
    #         download_m3u8(video_url, f"{HLS_OUTPUT_DIR}/video.tmp")
    #
    #     download_m3u8(audio_url, f"{HLS_OUTPUT_DIR}/audio.tmp")
    #     mux_video_audio(Path(HLS_OUTPUT_DIR, "video.tmp"), Path(HLS_OUTPUT_DIR, "audio.tmp"), f"{OUTPUT_DIR}/{output_filename}.mp4")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    asyncio.run(main())
