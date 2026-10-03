"""extracts the cdn links of the f2mc, stores them, and preper them to be used in cinema_cli"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import sys
import subprocess
import threading
import traceback
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import re
import warnings
import jdatetime
from bs4 import XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

MEDIA_EXTENSIONS = {
    '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.m4v', '.webm',
    '.mp3', '.wav', '.aac', '.zip', '.rar', '.7z', '.srt', '.vtt', '.mka'
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

class F2MC_DeepExtractor:
    def __init__(self, db_name="f2mc_database.db", threads=10):
        self.db_name = db_name
        self.threads = threads

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        self.queue = Queue()
        self.visited = set()
        self.visited_lock = threading.Lock()

        self.file_count = 0
        self.db_lock = threading.Lock()

        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.init_db()

    def init_db(self):
        c = self.conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                type TEXT,
                rate TEXT,
                imdb_rate TEXT,
                genres TEXT,
                description TEXT,
                source_url TEXT UNIQUE,
                finished INTEGER DEFAULT 0,
                country TEXT DEFAULT 'Unknown',
                avg_time INTEGER DEFAULT 0,
                published_year INTEGER DEFAULT 0,
                language TEXT DEFAULT 'Unknown',
                actors TEXT DEFAULT 'Unknown',
                directors TEXT DEFAULT 'Unknown'
            )
        ''')
        
        c.execute("PRAGMA table_info(media)")
        cols = [col[1] for col in c.fetchall()]
        new_cols = {
            'finished': "INTEGER DEFAULT 0",
            'country': "TEXT DEFAULT 'Unknown'",
            'avg_time': "INTEGER DEFAULT 0",
            'published_year': "INTEGER DEFAULT 0",
            'language': "TEXT DEFAULT 'Unknown'",
            'actors': "TEXT DEFAULT 'Unknown'",
            'directors': "TEXT DEFAULT 'Unknown'"
        }
        for col_name, col_type in new_cols.items():
            if col_name not in cols:
                c.execute(f"ALTER TABLE media ADD COLUMN {col_name} {col_type}")

        c.execute('''
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id INTEGER,
                season INTEGER,
                episode TEXT,
                quality TEXT,
                sub_status TEXT,
                url TEXT,
                FOREIGN KEY(media_id) REFERENCES media(id)
            )
        ''')
        self.conn.commit()

    def is_media(self, url):
        if not url: return False
        try:
            path = urlparse(url).path.lower()
            return any(path.endswith(ext) for ext in MEDIA_EXTENSIONS)
        except:
            return False

    def is_cloudflare_blocked(self, html): #no need for f2mc
        if not html: return True
        if "Just a moment..." in html or "challenge-platform" in html or "Enable JavaScript and cookies to continue" in html:
            return True
        return False

    def fetch_html(self, url):
        try:
            r = self.session.get(url, timeout=20, allow_redirects=True)
            if r.status_code == 200 and r.text:
                if not self.is_cloudflare_blocked(r.text):
                    return r.text
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["curl", "-s", "-L", "-A", HEADERS["User-Agent"], url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                if not self.is_cloudflare_blocked(result.stdout):
                    return result.stdout
        except Exception:
            pass

        return None

    def process_sitemap(self):
        print("[->] Fetching sitemap index...")
        sitemap_url = "https://www.f2mc.top/sitemap.xml"
        html = self.fetch_html(sitemap_url)

        if not html:
            print("[-] Failed to fetch sitemap.")
            return False

        soup = BeautifulSoup(html, "html.parser")
        sub_sitemaps = [loc.text for loc in soup.find_all("loc")]

        if not sub_sitemaps:
            return False

        print(f"[->] Found {len(sub_sitemaps)} sub-sitemaps. Reading them now...")
        for sub_url in sub_sitemaps:
            media_type = None
            if 'series-sitemap' in sub_url:
                media_type = None #'Series'
            elif 'post-sitemap' in sub_url:
                media_type = 'Movie'

            if media_type:
                print(f"  [.] Reading {media_type} sub-sitemap: {sub_url}")
                sub_html = self.fetch_html(sub_url)
                if sub_html:
                    sub_soup = BeautifulSoup(sub_html, "html.parser")
                    pages = [loc.text for loc in sub_soup.find_all("loc")]
                    for page in pages:
                        self.queue.put((page, media_type))
        return True

    def infer_sub_status(self, url):
        url_lower = url.lower()
        if "farsi.dubbed" in url_lower or "dubbed" in url_lower:
            return "Dubbed"
        elif "farsi.sub" in url_lower or "hardsub" in url_lower:
            return "Hardsub"
        return "Unknown"

    def extract_episode_number(self, text):
        match = re.search(r'[Ss]\d+[Ee](\d+)', text)
        if match: return match.group(1).zfill(2)
        match = re.search(r'[Ee]p?(\d+)', text)
        if match: return match.group(1).zfill(2)
        match = re.search(r'قسمت\s*(\d+)', text)
        if match: return match.group(1).zfill(2)

        stripped = text.replace("قسمت", "").strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            filename = urlparse(stripped).path.rsplit("/", 1)[-1]
            if re.search(r'trailer', filename, re.IGNORECASE):
                return "Trailer"
            return filename or "Unknown"
        return stripped if stripped else "Unknown"

    def extract_season_number(self, text, url=None):
        match = re.search(r'فصل\s*(\d+)', text)
        if match:
            return int(match.group(1))

        if url:
            match = re.search(r'[Ss](\d{1,2})[/\\]', url)
            if match:
                return int(match.group(1))
            match = re.search(r'[Ss](\d{1,2})[Ee]\d+', url)
            if match:
                return int(match.group(1))

        match = re.search(r'[Ss](\d{1,2})[Ee]\d+', text)
        if match:
            return int(match.group(1))

        return None

    def extract_quality_from_text(self, text):
        qualities = ['WEB-DL', 'BluRay', 'WEBRip', '2160p', '1080p', '720p', '480p', '4K', '10bit', 'x265', 'x264', 'Full HD']
        found = []
        text_lower = text.lower()
        for q in qualities:
            if q.lower() in text_lower:
                if q not in found:
                    found.append(q)
        if found:
            return " ".join(found)
        return "Unknown"

    def get_quality_and_sub(self, element):
        quality = "Unknown"
        sub_status = "Unknown"

        parent_li = element.find_parent("li")
        if parent_li:
            q_span = parent_li.find("span", class_="text")
            if q_span:
                q_text = q_span.text.strip()
                if q_text and q_text != "کیفیت :":
                    quality = q_text

        parent_dl_list = element.find_parent("div", class_="download-list")
        if parent_dl_list:
            classes = parent_dl_list.get("class", [])
            if "hardsub" in classes: sub_status = "Hardsub"
            elif "dubbled" in classes: sub_status = "Dubbed"
            elif "softsub" in classes: sub_status = "Softsub"

        return quality, sub_status

    def scrape_directory(self, dir_url, media_id, season, quality, sub_status, depth=0):
        if depth > 4:
            return

        print(f"  [.] Fetching directory: {dir_url}")
        html = self.fetch_html(dir_url)
        if not html:
            print(f"  [-] Failed to fetch directory (likely 403 Forbidden): {dir_url}")
            return

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a['href']
            if href.startswith("../") or href.startswith("?") or href.startswith("/"):
                continue

            full_url = urljoin(dir_url, href)

            if self.is_media(full_url):
                ep_num = self.extract_episode_number(full_url)
                file_season = self.extract_season_number(full_url, full_url)
                effective_season = file_season if file_season is not None else season

                dir_quality = quality
                if dir_quality == "Unknown":
                    dir_quality = self.extract_quality_from_text(full_url)

                dir_sub_status = sub_status
                if dir_sub_status == "Unknown":
                    dir_sub_status = self.infer_sub_status(full_url)

                self.save_link(media_id, effective_season, ep_num, dir_quality, dir_sub_status, full_url)

            elif full_url.endswith('/') and full_url != dir_url:
                sub_quality = quality
                if sub_quality == "Unknown":
                    sub_quality = self.extract_quality_from_text(full_url)
                sub_sub_status = sub_status
                if sub_sub_status == "Unknown":
                    sub_sub_status = self.infer_sub_status(full_url)
                sub_season = self.extract_season_number(full_url, full_url)
                if sub_season is None:
                    sub_season = season
                self.scrape_directory(full_url, media_id, sub_season, sub_quality, sub_sub_status, depth + 1)

    def scrape_page(self, url, media_type):
        try:
            self._scrape_page_inner(url, media_type)
        except Exception:
            print(f"  [!] EXCEPTION while scraping {url}")
            traceback.print_exc()

    def _scrape_page_inner(self, url, media_type):
        html = self.fetch_html(url)
        if not html:
            print(f"  [-] Blocked or failed to fetch: {url}")
            return

        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("h1", class_="entry-title")
        title = title_tag.text.strip() if title_tag else None

        if not title:
            print(f"  [-] No title found (likely blocked): {url}")
            return

        imdb_rate = "N/A"
        imdb_tag = soup.find("a", href=re.compile("imdb.com"))
        if imdb_tag and imdb_tag.find("strong"):
            imdb_rate = imdb_tag.find("strong").text.strip()

        rate = "N/A"
        rate_tag = soup.find("span", class_="feedback-satisfaction")
        if rate_tag:
            rate = rate_tag.text.strip()

        genres = []
        genres_container = soup.find("div", class_="entry-genres")
        if genres_container:
            for a in genres_container.find_all("a", class_="btn"):
                genres.append(a.text.strip())
        genres_str = ", ".join(genres)

        desc_tag = soup.find("div", class_="entry-excerpt")
        description = desc_tag.text.strip() if desc_tag else "N/A"

        finished = 0
        status_node = soup.find(string=re.compile(r"وضعیت\s*:"))
        if status_node:
            parent_div = status_node.find_parent("div")
            if parent_div and "به اتمام رسیده" in parent_div.get_text():
                finished = 1

        countries = []
        country_node = soup.find(string=re.compile(r"کشور سازنده\s*:"))
        if country_node:
            parent_div = country_node.find_parent("div")
            if parent_div:
                for a in parent_div.find_all("a", href=re.compile(r"/country/")):
                    countries.append(a.text.strip())
        country_str = ", ".join(countries) if countries else "Unknown"

        languages = []
        lang_node = soup.find(string=re.compile(r"زبان\s*:"))
        if lang_node:
            parent_div = lang_node.find_parent("div")
            if parent_div:
                for a in parent_div.find_all("a", href=re.compile(r"/language/")):
                    languages.append(a.text.strip())
        language_str = ", ".join(languages) if languages else "Unknown"

        avg_time = 0
        time_node = soup.find(string=re.compile(r"زمان\s*:"))
        if time_node:
            parent_div = time_node.find_parent("div")
            if parent_div:
                time_text = parent_div.get_text()
                match = re.search(r'(\d+)\s*دقیقه', time_text)
                if match:
                    avg_time = int(match.group(1))

        published_year = 0
        title_year_match = re.search(r'\b(19|20)\d{2}\b', title)
        if title_year_match:
            published_year = int(title_year_match.group(0))
        else:
            release_node = soup.find(string=re.compile(r"اکران\s*:"))
            if release_node:
                parent_div = release_node.find_parent("div")
                if parent_div:
                    date_span = parent_div.find("span", dir="ltr")
                    if date_span:
                        persian_date_str = date_span.text.strip()
                        try:
                            parts = persian_date_str.split('/')
                            if len(parts) == 3:
                                j_date = jdatetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
                                published_year = j_date.togregorian().year
                        except:
                            pass

        actors = []
        directors = []
        person_lists = soup.find_all("div", class_="persons-list")
        for p_list in person_lists:
            h3 = p_list.find("h3")
            if h3:
                header_text = h3.text.strip()
                names = [pn.text.strip() for pn in p_list.find_all("div", class_="person-name")]
                if "بازیگر" in header_text:
                    actors.extend(names)
                elif "کارگردان" in header_text:
                    directors.extend(names)
        
        actors_str = ", ".join(actors) if actors else "Unknown"
        directors_str = ", ".join(directors) if directors else "Unknown"


        media_id = self.save_media(
            title, media_type, rate, imdb_rate, genres_str, description, url, 
            finished, country_str, avg_time, published_year, language_str, actors_str, directors_str
        )
        if not media_id:
            print(f"  [=] Already in DB: {title}")
            return

        print(f"\n[->] SAVED {media_type}: {title} (IMDB: {imdb_rate}) [Year: {published_year}, Time: {avg_time}m]")

        season_divs = soup.find_all("div", class_="download-season")

        if season_divs:
            for season_idx, season_div in enumerate(season_divs, start=1):
                self.process_links_in_element(season_div, url, media_type, media_id, season_idx)
        else:
            content_root = (
                soup.find("section", id="downloads")
                or soup.find("div", class_="entry-content")
                or soup.find("article")
                or soup
            )
            default_season = 1 if media_type == 'Series' else 0
            self.process_links_in_element(content_root, url, media_type, media_id, default_season)

    def process_links_in_element(self, element, base_url, media_type, media_id, default_season):
        for a in element.find_all("a", href=True):
            link_text = a.get_text(strip=True)
            href = a['href'].strip()
            full_url = urljoin(base_url, href)

            is_directory = full_url.endswith('/') and "f2mc.top" not in full_url

            if media_type == 'Series':
                if "قسمت" in link_text or "فصل" in link_text or is_directory:
                    quality, sub_status = self.get_quality_and_sub(a)
                    if quality == "Unknown" or quality == "کیفیت :":
                        quality = self.extract_quality_from_text(link_text)

                    if sub_status == "Unknown":
                        sub_status = self.infer_sub_status(full_url)

                    link_season = self.extract_season_number(link_text, full_url)
                    if link_season is None:
                        link_season = default_season

                    if is_directory:
                        self.scrape_directory(full_url, media_id, link_season, quality, sub_status)
                    else:
                        ep_text = self.extract_episode_number(link_text)
                        self.save_link(media_id, link_season, ep_text, quality, sub_status, full_url)

            elif media_type == 'Movie':
                if is_directory or self.is_media(full_url):
                    quality, sub_status = self.get_quality_and_sub(a)
                    if quality == "Unknown" or quality == "کیفیت :":
                        quality = self.extract_quality_from_text(link_text)

                    if sub_status == "Unknown":
                        sub_status = self.infer_sub_status(full_url)
                        
                    if is_directory:
                        self.scrape_directory(full_url, media_id, 0, quality, sub_status)
                    else:
                        self.save_link(media_id, 0, "Full", quality, sub_status, full_url)

        for a in element.find_all("a", onclick=True):
            onclick_str = a.get("onclick", "")
            match = re.search(r"handleDownloadClick\(['\"](.*?)['\"]\)", onclick_str)
            if match:
                full_url = match.group(1)
                if self.is_media(full_url):
                    quality, sub_status = self.get_quality_and_sub(a)
                    if quality == "Unknown" or quality == "کیفیت :":
                        quality = self.extract_quality_from_text(full_url)
                    if sub_status == "Unknown":
                        sub_status = self.infer_sub_status(full_url)

                    if media_type == 'Series':
                        ep_text = self.extract_episode_number(full_url)
                        onclick_season = self.extract_season_number(full_url, full_url)
                        if onclick_season is None:
                            onclick_season = default_season
                        self.save_link(media_id, onclick_season, ep_text, quality, sub_status, full_url)
                    else:
                        self.save_link(media_id, 0, "Full", quality, sub_status, full_url)

    def save_media(self, title, media_type, rate, imdb_rate, genres, description, source_url, finished, country, avg_time, published_year, language, actors, directors):
        with self.db_lock:
            c = self.conn.cursor()
            c.execute("SELECT id FROM media WHERE source_url=?", (source_url,))
            row = c.fetchone()
            if row:
                return row[0]

            c.execute('''
                INSERT INTO media (title, type, rate, imdb_rate, genres, description, source_url, finished, country, avg_time, published_year, language, actors, directors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (title, media_type, rate, imdb_rate, genres, description, source_url, finished, country, avg_time, published_year, language, actors, directors))
            self.conn.commit()
            return c.lastrowid

    def save_link(self, media_id, season, episode, quality, sub_status, url):
        with self.db_lock:
            c = self.conn.cursor()
            c.execute("SELECT id FROM links WHERE media_id=? AND url=?", (media_id, url))
            if c.fetchone():
                return

            c.execute('''
                INSERT INTO links (media_id, season, episode, quality, sub_status, url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (media_id, season, episode, quality, sub_status, url))
            self.conn.commit()
            self.file_count += 1
            if season > 0:
                print(f"  [+] S{season}E{episode} | {quality} | {sub_status}")
            else:
                print(f"  [+] MOVIE | {quality} | {sub_status}")

    def run(self):
        print("[*] Starting F2MC Automated DB Builder...")

        if not self.process_sitemap():
            print("[-] Could not build queue from sitemap. Exiting.")
            return

        print(f"\n[*] Sitemap processed. {self.queue.qsize()} pages queued for deep scraping.")
        print("[*] Extracting media links and metadata...\n")

        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            futures = []
            while True:
                try:
                    url, m_type = self.queue.get(timeout=2)
                except Exception:
                    if self.queue.empty():
                        break
                    continue

                with self.visited_lock:
                    if url in self.visited:
                        self.queue.task_done()
                        continue
                    self.visited.add(url)

                futures.append(executor.submit(self.scrape_page, url, m_type))
                self.queue.task_done()

            for f in futures:
                f.result()

        self.conn.close()
        print("\n" + "="*50)
        print(f"[*] Deep Scrape Complete.")
        print(f"[*] Pages Visited: {len(self.visited)}")
        print(f"[*] Total Links Extracted: {self.file_count}")
        print(f"[*] Database saved to: {self.db_name}")

if __name__ == "__main__":
    extractor = F2MC_DeepExtractor()
    extractor.run()
